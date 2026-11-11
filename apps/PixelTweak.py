#!/usr/bin/env python3
"""
PixelTweak v3 — минималистичный пиксельный центр настроек для Ubuntu / GNOME.
Управляет: ZRAM, sysctl, earlyoom, gamemode, CPU (через sysfs), cacheclean (systemd-таймер).

Как работают права:
  * один раз ставится маленький системный помощник /usr/libexec/pixeltweak-helper
    и polkit-правило с нормальным описанием («PixelTweak хочет изменить настройки…»);
  * помощник умеет ТОЛЬКО заранее разрешённые операции (белый список файлов и команд);
  * каждое действие — один вызов pkexec, в фоне, с отчётом по каждому шагу;
  * перед каждой записью делается бэкап (+ «исходная» версия), есть откат;
  * всё пишется в журнал ~/.local/state/pixeltweak.log.
"""
import configparser
import glob
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime

try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, Gdk, Gio, GLib
except (ImportError, ValueError) as _e:
    sys.stderr.write(
        f"Не удалось загрузить GTK4 / Libadwaita ({_e}).\n"
        "Установите пакеты: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
    )
    sys.exit(1)

APP_ID = 'io.github.PixelTweak'
APP_NAME = 'PixelTweak'

# ══════════════════════════════════════════════════════════════════════
#  Системные части: helper, polkit-правило, скрипты (ставятся один раз)
# ══════════════════════════════════════════════════════════════════════
HELPER_PATH = "/usr/libexec/pixeltweak-helper"
CPU_APPLY_PATH = "/usr/libexec/pixeltweak-cpu-apply"
CPU_CONF = "/etc/pixeltweak/cpu.conf"
CPU_ORIG = "/etc/pixeltweak/cpu.orig.conf"
CPU_UNIT = "/etc/systemd/system/pixeltweak-cpu.service"
CACHECLEAN_SCRIPT = "/usr/share/cacheclean/cacheclean.sh"
POLICY_PATH = "/usr/share/polkit-1/actions/io.github.pixeltweak.policy"

# Код helper'а. Выполняется от root. Никаких произвольных команд: только белый список.
HELPER_SRC = r'''#!/usr/bin/python3
# PixelTweak root helper. Выполняет ТОЛЬКО операции из белого списка.
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

BACKUP_ROOT = "/var/lib/pixeltweak/backups"
KEEP = 10
HELPER_SELF = "/usr/libexec/pixeltweak-helper"
CPU_APPLY = "/usr/libexec/pixeltweak-cpu-apply"
CPU_ORIG = "/etc/pixeltweak/cpu.orig.conf"
CPU_UNIT_FILE = "/etc/systemd/system/pixeltweak-cpu.service"
CACHE_SH = "/usr/share/cacheclean/cacheclean.sh"
POLICY_FILE = "/usr/share/polkit-1/actions/io.github.pixeltweak.policy"
SYSCTL_CONF = "/etc/sysctl.d/99-pixeltweak.conf"
SYSCTL_KEY = r"vm\.(swappiness|vfs_cache_pressure|dirty_ratio|dirty_background_ratio)"

FILES = {
    "/etc/systemd/zram-generator.conf": "zram",
    SYSCTL_CONF: "sysctl",
    "/etc/default/earlyoom": "earlyoom",
    "/etc/gamemode.ini": "gamemode",
    "/etc/pixeltweak/cpu.conf": "cpuconf",
    CPU_ORIG: "cpuconf",
    CPU_UNIT_FILE: "unit",
    "/etc/systemd/system/cacheclean.service": "unit",
    "/etc/systemd/system/cacheclean.timer": "timer",
}

UNITS = {
    "cacheclean.timer", "cacheclean.service", "earlyoom.service",
    "pixeltweak-cpu.service", "dev-zram0.swap",
    "systemd-zram-setup@zram0.service", "power-profiles-daemon.service",
}

UNIT_LINES = {
    "[Unit]",
    "Description=PixelTweak CPU frequency settings",
    "Description=Clean page cache, dentries and inodes",
    "After=power-profiles-daemon.service tuned.service",
    "[Service]",
    "Type=oneshot",
    "RemainAfterExit=yes",
    "ExecStart=" + CPU_APPLY,
    "ExecStart=" + CACHE_SH,
    "[Install]",
    "WantedBy=multi-user.target",
}

TIMER_LINES = {
    "[Unit]",
    "Description=Periodic cache cleaning",
    "[Timer]",
    "OnBootSec=5min",
    "Unit=cacheclean.service",
    "[Install]",
    "WantedBy=timers.target",
}

GAMEMODE_BAD_KEYS = ("start", "end", "script")


def valid(kind, text):
    if not isinstance(text, str) or len(text) > 65536 or "\0" in text:
        return False
    lines = text.splitlines()
    if kind == "sysctl":
        return all(re.fullmatch(r"\s*|#.*|" + SYSCTL_KEY + r" = \d{1,4}", l) for l in lines)
    if kind == "cpuconf":
        return all(re.fullmatch(r"\s*|#.*|GOVERNOR=[a-z_]+|MIN_KHZ=\d+|MAX_KHZ=\d+", l) for l in lines)
    if kind == "unit":
        return all(not l.strip() or l.strip() in UNIT_LINES for l in lines)
    if kind == "timer":
        for l in lines:
            s = l.strip()
            if not s or s in TIMER_LINES:
                continue
            if re.fullmatch(r"OnUnitActiveSec=(15min|1h|3h|6h|12h|24h)", s):
                continue
            return False
        return True
    if kind == "zram":
        for l in lines:
            s = l.strip()
            if not s or s.startswith(("#", ";")):
                continue
            if re.fullmatch(r"\[zram\d+\]", s):
                continue
            if re.fullmatch(
                r"(zram-size|compression-algorithm|swap-priority|host-memory-limit|fs-type)"
                r"\s*=\s*[A-Za-z0-9_.,()+*/%^= -]+", s
            ):
                continue
            return False
        return True
    if kind == "gamemode":
        # Разрешён заголовок [custom] (он есть в стандартном примере), но ключи
        # start/end/script и любые ключи внутри [custom] (там запускаются команды) — нет.
        sec = ""
        for l in lines:
            s = l.strip()
            if not s or s.startswith((";", "#")):
                continue
            m = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]", s)
            if m:
                sec = m.group(1).lower()
                continue
            km = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*([^\x00-\x1f`$\\]*)", s)
            if not km or not sec:
                return False
            if sec == "custom" or km.group(1).lower() in GAMEMODE_BAD_KEYS:
                return False
        return True
    if kind == "earlyoom":
        # Только EARLYOOM_ARGS (файл читается systemd как EnvironmentFile, другие
        # переменные вроде LD_PRELOAD недопустимы) и без -N / -P (запуск скриптов).
        for l in lines:
            s = l.strip()
            if not s or s.startswith("#"):
                continue
            m = re.fullmatch(r"""EARLYOOM_ARGS=(?:"([^"\\`]*)"|([^"'\\`\s]*))""", s)
            if not m:
                return False
            body = m.group(1) if m.group(1) is not None else m.group(2)
            if "$(" in body or "${" in body:
                return False
            try:
                toks = shlex.split(body)
            except ValueError:
                return False
            for t in toks:
                if t.startswith("-") and not t.startswith("--") and re.search(r"[NP]", t):
                    return False
        return True
    return False


def cmd_allowed(cmd):
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        return False
    if cmd == ["systemctl", "daemon-reload"]:
        return True
    if cmd[0] == "systemctl" and len(cmd) >= 3 and cmd[1] in (
        "enable", "disable", "start", "stop", "restart", "mask", "unmask"
    ):
        flags = [x for x in cmd[2:] if x.startswith("-")]
        units = [x for x in cmd[2:] if not x.startswith("-")]
        return all(f == "--now" for f in flags) and bool(units) and all(u in UNITS for u in units)
    if cmd in (["sysctl", "-p", SYSCTL_CONF], ["sysctl", "--system"]):
        return True
    if len(cmd) == 3 and cmd[:2] == ["sysctl", "-w"]:
        return re.fullmatch(SYSCTL_KEY + r"=\d{1,4}", cmd[2]) is not None
    return cmd in ([CPU_APPLY], [CPU_APPLY, CPU_ORIG], [CPU_APPLY, "--reset"], [CACHE_SH])


def bdir(path):
    return os.path.join(BACKUP_ROOT, path.strip("/").replace("/", "__"))


def make_backup(path):
    d = bdir(path)
    os.makedirs(d, exist_ok=True)
    existed = os.path.exists(path)
    orig = os.path.join(d, "orig")
    none = os.path.join(d, "orig.none")
    if not os.path.exists(orig) and not os.path.exists(none):
        if existed:
            shutil.copy2(path, orig)
        else:
            open(none, "w").close()
    if existed:
        shutil.copy2(path, os.path.join(d, "v-%013d" % int(time.time() * 1000)))
        versions = sorted(x for x in os.listdir(d) if x.startswith("v-"))
        for old in versions[:-KEEP]:
            os.remove(os.path.join(d, old))


def atomic_write(path, content):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pixeltweak-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def do_write(op):
    path = op["path"]
    kind = FILES.get(path)
    if kind is None:
        raise ValueError("путь не разрешён")
    if not valid(kind, op["content"]):
        raise ValueError("содержимое не прошло проверку (тип: %s)" % kind)
    if op.get("only_if_missing") and os.path.exists(path):
        return "уже существует"
    make_backup(path)
    atomic_write(path, op["content"])
    return ""


def do_restore(op):
    path = op["path"]
    if path not in FILES:
        raise ValueError("путь не разрешён")
    d = bdir(path)
    mode = op.get("mode")
    if mode == "last":
        versions = sorted(x for x in os.listdir(d) if x.startswith("v-")) if os.path.isdir(d) else []
        if not versions:
            raise ValueError("нет сохранённых версий")
        source = os.path.join(d, versions[-1])
        with open(source, "r", encoding="utf-8") as fh:
            text = fh.read()
        make_backup(path)
        atomic_write(path, text)
        return "восстановлена предыдущая версия"
    if mode == "orig":
        orig = os.path.join(d, "orig")
        if os.path.exists(orig):
            with open(orig, "r", encoding="utf-8") as fh:
                text = fh.read()
            make_backup(path)
            atomic_write(path, text)
            return "восстановлена исходная версия"
        if os.path.exists(os.path.join(d, "orig.none")):
            if os.path.exists(path):
                make_backup(path)
                os.remove(path)
            return "файла изначально не было — удалён"
        raise ValueError("исходной версии нет (программа ещё не меняла этот файл)")
    raise ValueError("неизвестный режим")


def do_remove(op):
    path = op["path"]
    if path not in FILES:
        raise ValueError("путь не разрешён")
    if not os.path.exists(path):
        return "файла нет"
    make_backup(path)
    os.remove(path)
    return ""


def do_cmd(op):
    cmd = op["cmd"]
    if not cmd_allowed(cmd):
        raise ValueError("команда не разрешена")
    timeout = min(int(op.get("timeout", 60)), 300)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError("таймаут %d с (команда прервана)" % timeout)
    if r.returncode != 0:
        out = (r.stderr or r.stdout).strip().replace("\n", " ")[:300]
        raise ValueError(out or "код выхода %d" % r.returncode)
    return ""


def do_uninstall():
    # Скрипт применения CPU нужен сервису pixeltweak-cpu.service — пока сервис есть, не трогаем.
    paths = [POLICY_FILE, HELPER_SELF]
    if not os.path.exists(CPU_UNIT_FILE):
        paths.insert(1, CPU_APPLY)
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
    return "помощник и polkit-правило удалены (настройки, сервисы и бэкапы не тронуты)"


def describe(op):
    kind = op.get("op")
    if kind == "write":
        return "запись " + str(op.get("path"))
    if kind == "restore":
        return "восстановление %s (%s)" % (op.get("path"), op.get("mode"))
    if kind == "remove":
        return "удаление " + str(op.get("path"))
    if kind == "cmd":
        return " ".join(str(x) for x in op.get("cmd", []))
    if kind == "uninstall_helper":
        return "удаление системного помощника"
    return "неизвестная операция"


def run_ops(ops):
    results = []
    failed = False
    for op in ops:
        entry = {"step": describe(op), "ok": True, "msg": "", "skipped": False}
        try:
            if op.get("requires_prev") and failed:
                entry.update(ok=False, skipped=True, msg="пропущено: предыдущий шаг не удался")
            elif op.get("if_exists") and not os.path.exists(op["if_exists"]):
                entry.update(skipped=True, msg="пропущено: нет " + op["if_exists"])
            elif op.get("if_missing") and os.path.exists(op["if_missing"]):
                entry.update(skipped=True, msg="пропущено: есть " + op["if_missing"])
            else:
                kind = op.get("op")
                if kind == "write":
                    entry["msg"] = do_write(op)
                elif kind == "restore":
                    entry["msg"] = do_restore(op)
                elif kind == "remove":
                    entry["msg"] = do_remove(op)
                elif kind == "cmd":
                    entry["msg"] = do_cmd(op)
                elif kind == "uninstall_helper":
                    entry["msg"] = do_uninstall()
                else:
                    raise ValueError("неизвестная операция")
        except Exception as e:
            if op.get("ignore"):
                entry.update(ok=True, msg="ошибка проигнорирована: " + str(e))
            else:
                entry.update(ok=False, msg=str(e))
        if not entry["ok"]:
            failed = True
        results.append(entry)
    return results


def main():
    if os.geteuid() != 0:
        print(json.dumps([{"step": "helper", "ok": False, "msg": "нужны права root", "skipped": False}], ensure_ascii=False))
        return 0
    try:
        ops = json.loads(sys.stdin.read(1000000))["ops"]
    except Exception as e:
        print(json.dumps([{"step": "разбор задания", "ok": False, "msg": str(e), "skipped": False}], ensure_ascii=False))
        return 0
    print(json.dumps(run_ops(ops), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# Применяет настройки CPU напрямую через sysfs (без cpupower!). Порядок min/max
# и границы учитываются для каждого ядра отдельно — корректно для P/E-ядер.
CPU_APPLY_SRC = r'''#!/bin/sh
# PixelTweak: применить политику CPU через sysfs. Аргумент: файл настроек или --reset.
if [ "${1:-}" = "--reset" ]; then
    rc=0
    for d in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
        [ -d "$d" ] || continue
        avail=$(cat "$d/scaling_available_governors" 2>/dev/null || echo "")
        for g in schedutil powersave ondemand performance; do
            case " $avail " in
                *" $g "*) echo "$g" > "$d/scaling_governor" 2>/dev/null || rc=1; break ;;
            esac
        done
        hw_min=$(cat "$d/cpuinfo_min_freq" 2>/dev/null || echo 0)
        hw_max=$(cat "$d/cpuinfo_max_freq" 2>/dev/null || echo 0)
        cur_max=$(cat "$d/scaling_max_freq" 2>/dev/null || echo 0)
        if [ "$hw_min" -gt 0 ] && [ "$hw_max" -gt 0 ]; then
            if [ "$hw_min" -gt "$cur_max" ]; then
                echo "$hw_max" > "$d/scaling_max_freq" || rc=1
                echo "$hw_min" > "$d/scaling_min_freq" || rc=1
            else
                echo "$hw_min" > "$d/scaling_min_freq" || rc=1
                echo "$hw_max" > "$d/scaling_max_freq" || rc=1
            fi
        fi
    done
    exit $rc
fi

CONF="${1:-/etc/pixeltweak/cpu.conf}"
if [ ! -r "$CONF" ]; then
    echo "Файл настроек не найден: $CONF" >&2
    exit 1
fi
GOVERNOR=""; MIN_KHZ=""; MAX_KHZ=""
. "$CONF"
rc=0
for d in /sys/devices/system/cpu/cpu[0-9]*/cpufreq; do
    [ -d "$d" ] || continue
    if [ -n "$GOVERNOR" ] && [ -w "$d/scaling_governor" ]; then
        echo "$GOVERNOR" > "$d/scaling_governor" || rc=1
    fi
    if [ -n "$MIN_KHZ" ] && [ -n "$MAX_KHZ" ]; then
        hw_min=$(cat "$d/cpuinfo_min_freq" 2>/dev/null || echo 0)
        hw_max=$(cat "$d/cpuinfo_max_freq" 2>/dev/null || echo 0)
        cur_max=$(cat "$d/scaling_max_freq" 2>/dev/null || echo 0)
        lo=$MIN_KHZ; hi=$MAX_KHZ
        [ "$hw_min" -gt 0 ] && [ "$lo" -lt "$hw_min" ] && lo=$hw_min
        [ "$hw_max" -gt 0 ] && [ "$hi" -gt "$hw_max" ] && hi=$hw_max
        [ "$lo" -gt "$hi" ] && lo=$hi
        if [ "$lo" -gt "$cur_max" ]; then
            echo "$hi" > "$d/scaling_max_freq" || rc=1
            echo "$lo" > "$d/scaling_min_freq" || rc=1
        else
            echo "$lo" > "$d/scaling_min_freq" || rc=1
            echo "$hi" > "$d/scaling_max_freq" || rc=1
        fi
    fi
done
exit $rc
'''

CACHE_SCRIPT_SRC = "#!/bin/sh\nsync\necho 3 > /proc/sys/vm/drop_caches\n"

POLICY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1.0/policyconfig.dtd">
<policyconfig>
  <vendor>PixelTweak</vendor>
  <action id="io.github.PixelTweak.apply">
    <description>Change system performance settings</description>
    <description xml:lang="ru">Изменить системные настройки производительности</description>
    <message>PixelTweak wants to change system performance settings (ZRAM, kernel, CPU, EarlyOOM, GameMode, cache cleaning)</message>
    <message xml:lang="ru">PixelTweak хочет изменить настройки производительности (ZRAM, ядро, CPU, EarlyOOM, GameMode, очистка кэша)</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/libexec/pixeltweak-helper</annotate>
  </action>
</policyconfig>
"""

# Что должно лежать в системе (путь, содержимое, права)
BOOTSTRAP_FILES = [
    (HELPER_PATH, HELPER_SRC, 0o755),
    (CPU_APPLY_PATH, CPU_APPLY_SRC, 0o755),
    (CACHECLEAN_SCRIPT, CACHE_SCRIPT_SRC, 0o755),
    (POLICY_PATH, POLICY_XML, 0o644),
]

# Одноразовая установка (единственный «страшный» запрос пароля — перед ним показывается объяснение)
BOOTSTRAP_CODE = r'''
import json, os, sys, tempfile
for f in json.load(sys.stdin):
    d = os.path.dirname(f["path"])
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pixeltweak-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f["content"])
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, f["mode"])
    os.chown(tmp, 0, 0)
    os.replace(tmp, f["path"])
'''

# ══════════════════════════════════════════════════════════════════════
#  CSS — пиксельный стиль. Цвета подставляются Python'ом (без @define-color)
# ══════════════════════════════════════════════════════════════════════
COLORS = {
    "window_bg":     "#191424",
    "panel_bg":      "#241c34",
    "panel_bg2":     "#2b2140",
    "border_color":  "#4a3d5a",
    "accent_purple": "#a259ff",
    "accent_orange": "#ff8800",
    "text_neutral":  "#e4dcf0",
    "text_muted":    "#b5a8c8",
}

CSS_TEMPLATE = """
/* ─── База окна ─────────────────────────────────────────────── */
window, .background {
    background-color: @window_bg;
    color: @text_neutral;
}

headerbar {
    background-color: @panel_bg;
    border-bottom: 2px solid @border_color;
    box-shadow: none;
}

/* ─── Квадратные элементы ──────────────────────────────────── */
button, entry, spinbutton, dropdown, row, .card,
preferencesgroup, preferencespage, listbox, menubutton > button {
    border-radius: 0;
    box-shadow: none;
}

button {
    border: 1px solid @border_color;
    background-color: @panel_bg2;
    color: @text_neutral;
    font-weight: 600;
    transition: 80ms ease;
    box-shadow: none;
    outline: none;
}

button:hover {
    background-color: @accent_purple;
    color: #ffffff;
    border-color: @accent_purple;
}

button:disabled {
    opacity: 0.5;
}

button.suggested-action {
    background-color: @accent_purple;
    color: #ffffff;
    border-color: @accent_purple;
}
button.suggested-action:hover {
    background-color: @accent_orange;
    color: #000000;
    border-color: @accent_orange;
}

button.destructive-action {
    background-color: @accent_orange;
    color: #000000;
    border-color: @accent_orange;
}

/* ─── Switch (ползунок) ────────────────────────────────────── */
switch {
    background-color: @panel_bg2;
    border: 1px solid @border_color;
    border-radius: 0;
    box-shadow: none;
    outline: none;
    min-width: 46px;
    min-height: 24px;
}
switch:checked {
    background-color: @accent_purple;
    border-color: @accent_purple;
}
switch > slider, switch > image, switch > * {
    background-color: @text_neutral;
    border-radius: 0;
    border: none;
    box-shadow: none;
    min-width: 18px;
    min-height: 18px;
}
switch:checked > slider,
switch:checked > image,
switch:checked > * {
    background-color: #ffffff;
}

/* ─── Entry / SpinButton ───────────────────────────────────── */
entry, spinbutton {
    background-color: @panel_bg2;
    border: 1px solid @border_color;
    color: @text_neutral;
    font-family: monospace;
    box-shadow: none;
    outline: none;
}

entry > text, spinbutton > text {
    background-color: transparent;
    color: @text_neutral;
    box-shadow: none;
    outline: none;
    border: none;
}

entry:focus, entry:focus-within,
spinbutton:focus, spinbutton:focus-within,
entry:focus-visible, spinbutton:focus-visible {
    outline: none;
    box-shadow: none;
    border: 1px solid @accent_purple;
}

/* ─── SpinButton: кнопки +/− ───────────────────────────────── */
spinbutton > button, spinbutton button {
    background-color: @panel_bg;
    border: 1px solid @border_color;
    color: @text_neutral;
    border-radius: 0;
    box-shadow: none;
    outline: none;
    min-width: 24px;
    min-height: 24px;
}
spinbutton > button:hover, spinbutton button:hover {
    background-color: @accent_purple;
    color: #ffffff;
    border-color: @accent_purple;
}
spinbutton > button > image, spinbutton > button image {
    color: @text_neutral;
    background: transparent;
}

/* ─── Popover ──────────────────────────────────────────────── */
popover,
popover.background,
popover.menu,
popover.menu.background,
.background.popover,
dropdown popover,
dropdown.popover,
.combo popover,
.combo.popover,
menubutton popover,
menubutton.popover,
spinbutton popover,
spinbutton.popover {
    background-color: @panel_bg2;
    color: @text_neutral;
    border: 1px solid @border_color;
    border-radius: 0;
    box-shadow: none;
    padding: 0;
}

popover > contents,
popover.background > contents {
    background-color: transparent;
    border: none;
    border-radius: 0;
    box-shadow: none;
    padding: 4px;
}

/* Стрелка поповера (скрыта через CSS + set_has_arrow(False)) */
popover > arrow,
popover.background > arrow,
popover.menu > arrow {
    opacity: 0;
    min-width: 0;
    min-height: 0;
    padding: 0;
    margin: 0;
    border: none;
    background: transparent;
}

popover *,
popover.background *,
dropdown popover *,
.combo popover * {
    box-shadow: none;
    outline: none;
}

/* Строки внутри поповера */
popover modelbutton,
popover menuitem,
popover listview,
popover listview row,
popover list,
popover list row,
dropdown popover row,
.combo popover row {
    background-color: transparent;
    color: @text_neutral;
    border-radius: 0;
    box-shadow: none;
    padding: 6px 10px;
}

popover modelbutton:hover,
popover menuitem:hover,
popover listview row:hover,
popover list row:hover,
dropdown popover row:hover,
.combo popover row:hover {
    background-color: @accent_purple;
    color: #ffffff;
}

popover modelbutton:checked,
popover menuitem:checked,
popover listview row:selected,
popover list row:selected,
dropdown popover row:selected,
.combo popover row:selected {
    background-color: @accent_purple;
    color: #ffffff;
}

/* ─── Чекбоксы / радио ─────────────────────────────────────── */
checkbutton check,
check,
radio {
    background-color: @panel_bg;
    border: 2px solid @border_color;
    border-radius: 0;
    box-shadow: none;
    color: transparent;
    min-width: 14px;
    min-height: 14px;
}

checkbutton check:checked,
check:checked,
radio:checked {
    background-color: @accent_purple;
    border-color: @accent_purple;
    color: #ffffff;
}

checkbutton check:indeterminate,
check:indeterminate {
    background-color: @accent_orange;
    border-color: @accent_orange;
    color: #000000;
}

/* ─── Скроллбар ────────────────────────────────────────────── */
scrollbar, scrollbar > range, scrollbar > range > trough {
    background-color: transparent;
    border-radius: 0;
    box-shadow: none;
}
scrollbar slider {
    background-color: @border_color;
    border-radius: 0;
    border: none;
    box-shadow: none;
    min-width: 6px;
    min-height: 6px;
}
scrollbar slider:hover {
    background-color: @accent_purple;
}

/* ─── Строки и группы ──────────────────────────────────────── */
row {
    background-color: @panel_bg;
    border-bottom: 1px solid @border_color;
}

row.activatable:hover,
row:focus {
    background-color: @panel_bg2;
    outline: none;
    box-shadow: none;
}

preferencesgroup > box > label {
    color: @accent_purple;
    font-weight: 700;
}

/* Строка статуса */
row.status-row {
    background-color: @panel_bg2;
}
row.status-row > box > label.subtitle {
    color: @accent_purple;
    font-family: monospace;
    font-weight: 600;
}
row.status-row > box > label.title {
    color: @text_muted;
    font-size: 0.85em;
    font-weight: 500;
}

/* ─── Выделение текста ─────────────────────────────────────── */
selection, *:selected {
    background-color: @accent_purple;
    color: #ffffff;
}

/* ─── Тост / tooltip / окно диалога ────────────────────────── */
toast, toast > widget {
    background-color: @panel_bg2;
    color: @text_neutral;
    border: 1px solid @accent_purple;
    border-radius: 0;
    box-shadow: none;
}

tooltip, tooltip > box, tooltip > box > label {
    background-color: @panel_bg2;
    color: @text_neutral;
    border: 1px solid @border_color;
    border-radius: 0;
    box-shadow: none;
}

dialog, .dialog, window.dialog, window.dialog .background {
    background-color: @window_bg;
    border-radius: 0;
    box-shadow: none;
}

/* ─── Кнопки управления окном ──────────────────────────────── */
windowcontrols > button {
    border-radius: 0;
    background-color: transparent;
    border: 1px solid transparent;
}
windowcontrols > button:hover {
    background-color: @accent_purple;
    border-color: @accent_purple;
}
windowcontrols > button > image {
    color: @text_neutral;
}
"""

CSS = re.sub(r"@(\w+)", lambda m: COLORS[m.group(1)], CSS_TEMPLATE)

# ══════════════════════════════════════════════════════════════════════
#  Хелперы: журнал, toast, диалоги, фоновые задачи
# ══════════════════════════════════════════════════════════════════════
_toast_overlay = None
_busy = False
_ACTION_BUTTONS = []
_cancel_btn = None
_current_proc = None
_job_seq = 0
_LOG_LOCK = threading.Lock()
JOB_TIMEOUT = 180  # для установки помощника (с запасом на ввод пароля)

LOG_PATH = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "pixeltweak.log",
)


def read_file(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def log_write(text: str):
    with _LOG_LOCK:
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
                tail = read_file(LOG_PATH)[-200000:]
                with open(LOG_PATH, "w", encoding="utf-8") as f:
                    f.write(tail)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        except OSError:
            pass


def toast(text: str, details: bool = False):
    """details=True добавляет кнопку «Подробности» (открывает журнал)."""
    if _toast_overlay is None:
        return
    t = Adw.Toast.new(text[:200])
    if hasattr(t, "set_use_markup"):
        t.set_use_markup(False)
    if details:
        t.set_button_label("Подробности")
        t.set_action_name("win.show-log")
        t.set_timeout(10)
    _toast_overlay.add_toast(t)


def ask(parent, heading, body, options, on_choice, extra=None, cancel_label="Отмена"):
    """Диалог. options: [(id, подпись, destructive)]. on_choice(id) — при выборе не-«отмены»."""
    use_alert = hasattr(Adw, "AlertDialog")
    if use_alert:
        d = Adw.AlertDialog(heading=heading, body=body)
    else:
        d = Adw.MessageDialog(transient_for=parent, heading=heading, body=body)
    d.add_response("cancel", cancel_label)
    for rid, label, destructive in options:
        d.add_response(rid, label)
        if destructive:
            d.set_response_appearance(rid, Adw.ResponseAppearance.DESTRUCTIVE)
    d.set_default_response("cancel")
    d.set_close_response("cancel")
    if extra is not None:
        d.set_extra_child(extra)

    def on_resp(_d, rid):
        if rid != "cancel" and on_choice is not None:
            on_choice(rid)

    d.connect("response", on_resp)
    if use_alert:
        d.present(parent)
    else:
        d.present()


def show_log(parent):
    text = read_file(LOG_PATH, "").strip()[-12000:] or "Журнал пуст."
    view = Gtk.TextView()
    view.set_editable(False)
    view.set_cursor_visible(False)
    view.set_monospace(True)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    buf = view.get_buffer()
    buf.set_text(text)
    scroll = Gtk.ScrolledWindow()
    scroll.set_min_content_height(300)
    scroll.set_min_content_width(520)
    scroll.set_child(view)

    def to_end():
        view.scroll_to_iter(buf.get_end_iter(), 0.0, False, 0.0, 0.0)
        return False
    GLib.idle_add(to_end)

    opts = []
    if os.path.exists(HELPER_PATH) or os.path.exists(POLICY_PATH):
        opts.append(("uninstall", "Удалить помощник", True))

    def on_choice(rid):
        if rid != "uninstall":
            return

        def really(_r):
            apply_job(None, [{"op": "uninstall_helper"}],
                      "Системный помощник и polkit-правило удалены",
                      "Удаление помощника")
        ask(parent, "Удалить системный помощник?",
            "Будут удалены /usr/libexec/pixeltweak-helper и polkit-правило. Настройки, "
            "сервисы и бэкапы останутся. При следующем действии программа предложит "
            "установить помощник заново.",
            [("rm", "Удалить", True)], really)

    ask(parent, "Журнал PixelTweak", LOG_PATH, opts, on_choice,
        extra=scroll, cancel_label="Закрыть")


def bg(func, done=None):
    """func() выполняется в фоне, done(результат) — в главном потоке."""
    def worker():
        try:
            res = func()
        except Exception as e:  # noqa: BLE001
            res = e
        if done is not None:
            GLib.idle_add(lambda: (done(res), False)[1])
    threading.Thread(target=worker, daemon=True).start()


# ── блокировка параллельных root-задач + отмена ──────────────────────
def btn_enable(btn, enabled: bool):
    btn._pt_enabled = enabled
    btn.set_sensitive(enabled and not _busy)


def set_busy(value: bool):
    global _busy
    _busy = value
    for b in _ACTION_BUTTONS:
        b.set_sensitive((not value) and getattr(b, "_pt_enabled", True))
    if _cancel_btn is not None:
        _cancel_btn.set_visible(value)


def cancel_current_job(_btn=None):
    """Отмена ожидания. Надёжно останавливает только окно пароля: если helper уже
    запущен от root, он дорабатывает сам, а его отчёт попадёт в журнал."""
    global _current_proc, _job_seq
    _job_seq += 1
    proc = _current_proc
    _current_proc = None
    if proc is not None:
        proc._pt_cancelled = True
        try:
            proc.terminate()
        except OSError:
            pass
    set_busy(False)
    log_write("операция отменена пользователем")
    toast("Запрос отменён. Если пароль уже был введён, часть шагов могла выполниться — см. журнал",
          details=True)


# ── операции для helper'а ────────────────────────────────────────────
def W(path, content, only_if_missing=False):
    return {"op": "write", "path": path, "content": content, "only_if_missing": only_if_missing}


def R(path, mode):
    return {"op": "restore", "path": path, "mode": mode}


def D(path):
    return {"op": "remove", "path": path}


def C(cmd, **flags):
    op = {"op": "cmd", "cmd": cmd}
    op.update(flags)
    return op


def helper_ok() -> bool:
    return all(read_file(p) == src for p, src, _m in BOOTSTRAP_FILES)


def job_timeout(ops: list) -> int:
    """180 с на ввод пароля + сумма таймаутов команд (иначе клиент оборвётся раньше helper'а)."""
    total = 180
    for op in ops:
        if op.get("op") == "cmd":
            try:
                total += min(int(op.get("timeout", 60)), 300)
            except (TypeError, ValueError):
                total += 60
    return total


def run_root(ops: list, timeout=None, seq=None) -> dict:
    """Один pkexec на всё действие, с таймаутом и поддержкой отмены."""
    global _current_proc
    if timeout is None:
        timeout = job_timeout(ops)
    if seq is not None and seq != _job_seq:
        return {"status": "cancel", "results": []}
    try:
        proc = subprocess.Popen(
            ["pkexec", HELPER_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return {"status": "error", "error": "pkexec не найден", "results": []}
    _current_proc = proc
    if seq is not None and seq != _job_seq:  # отмена прилетела, пока запускались
        proc._pt_cancelled = True
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        stdout, stderr = proc.communicate(input=json.dumps({"ops": ops}), timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.communicate(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error",
                "error": f"превышен таймаут ожидания ({timeout} с); helper мог продолжить работу — см. журнал",
                "results": []}
    except Exception as e:  # noqa: BLE001
        if getattr(proc, "_pt_cancelled", False):
            return {"status": "cancel", "results": []}
        return {"status": "error", "error": str(e), "results": []}
    finally:
        if _current_proc is proc:
            _current_proc = None

    try:
        results = json.loads(stdout)
        if not isinstance(results, list):
            raise ValueError("не список")
    except ValueError:
        results = None

    if getattr(proc, "_pt_cancelled", False) or proc.returncode in (126, 127, -15, -9):
        return {"status": "cancel", "results": results or []}
    if results is None:
        err = (stderr.strip().splitlines() or [f"код выхода {proc.returncode}"])[-1]
        return {"status": "error", "error": err, "results": [], "stderr": stderr}
    return {"status": "ok", "results": results}


def summarize(res: dict):
    """(успех?, краткий текст ошибки)."""
    if res["status"] == "cancel":
        return False, "отменено (пароль не введён)"
    if res["status"] == "error":
        return False, res.get("error", "неизвестная ошибка")
    results = res["results"]
    bad = [r for r in results if not r.get("ok")]
    if not bad:
        return True, ""
    done_n = sum(1 for r in results if r.get("ok") and not r.get("skipped"))
    parts = [f"{r['step']}: {r['msg']}" if r.get("msg") else r["step"] for r in bad]
    return False, f"выполнено {done_n} из {len(results)}; не удалось: " + "; ".join(parts)


def log_job(title: str, res: dict):
    lines = [f"── {title}: {res['status']}"]
    if res.get("error"):
        lines.append(f"   ошибка: {res['error']}")
    if res.get("stderr"):
        lines.append("   stderr: " + res["stderr"].strip().replace("\n", " | "))
    for r in res.get("results", []):
        mark = "OK  " if r.get("ok") else "FAIL"
        if r.get("skipped"):
            mark = "SKIP"
        tail = f" — {r['msg']}" if r.get("msg") else ""
        lines.append(f"   [{mark}] {r['step']}{tail}")
    log_write("\n".join(lines))


def apply_job(btn, ops, ok_text, fail_text, after=None):
    """Выполнить root-задачу: проверка helper'а → один pkexec → отчёт по шагам → журнал."""
    if _busy:
        toast("Подождите: другая операция ещё выполняется")
        return
    parent = btn.get_root() if btn is not None else None

    def start():
        global _job_seq
        _job_seq += 1
        my_seq = _job_seq
        set_busy(True)

        def work():
            res = run_root(ops, seq=my_seq)
            log_job(fail_text, res)
            return res

        def done(res):
            if my_seq != _job_seq:  # задачу отменили — интерфейс уже свободен
                return
            set_busy(False)
            if isinstance(res, Exception):
                res = {"status": "error", "error": str(res), "results": []}
            ok, msg = summarize(res)
            if ok:
                toast(ok_text)
            else:
                toast(f"{fail_text}: {msg}", details=res["status"] != "cancel")
            if after is not None:
                after(ok)

        bg(work, done)

    if helper_ok():
        start()
    else:
        install_helper(parent, start)


def install_helper(parent, then):
    body = (
        "Чтобы менять системные настройки, PixelTweak один раз устанавливает небольшой "
        f"системный помощник ({HELPER_PATH}) и правило polkit. Помощник умеет только заранее "
        "разрешённые операции (файлы настроек и несколько команд systemctl/sysctl).\n\n"
        "Сейчас система спросит пароль для установки — в этом окне будет упомянут python3, это нормально. "
        "Дальше окно пароля будет называться «PixelTweak хочет изменить настройки производительности»."
    )

    def go(_rid):
        global _job_seq, _current_proc
        _job_seq += 1
        my_seq = _job_seq
        set_busy(True)
        files = [{"path": p, "content": s, "mode": m} for p, s, m in BOOTSTRAP_FILES]

        def work():
            global _current_proc
            if my_seq != _job_seq:
                return -1, "отменено"
            proc = subprocess.Popen(
                ["pkexec", "/usr/bin/python3", "-c", BOOTSTRAP_CODE],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            _current_proc = proc
            if my_seq != _job_seq:
                proc._pt_cancelled = True
                try:
                    proc.terminate()
                except OSError:
                    pass
            try:
                _out, err = proc.communicate(input=json.dumps(files), timeout=JOB_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.communicate(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
                return -1, f"превышен таймаут ({JOB_TIMEOUT} с)"
            finally:
                if _current_proc is proc:
                    _current_proc = None
            log_write(f"установка helper: код {proc.returncode} {err.strip()}")
            return proc.returncode, err

        def done(res):
            if my_seq != _job_seq:
                return
            set_busy(False)
            if isinstance(res, Exception):
                toast(f"Установка помощника: {res}", details=True)
                return
            rc, err = res
            if rc == 0 and helper_ok():
                toast("Помощник установлен")
                then()
            elif rc in (126, 127):
                toast("Установка отменена")
            else:
                last = (err.strip().splitlines() or [f"код {rc}"])[-1]
                toast(f"Не удалось установить помощник: {last}", details=True)

        bg(work, done)

    ask(parent, "Одноразовая установка помощника", body,
        [("install", "Установить", False)], go)


def service_active(name: str) -> bool:
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", name],
            timeout=2,
        ).returncode == 0
    except Exception:
        return False


def service_masked(name: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-enabled", name],
            capture_output=True, text=True, timeout=2,
        )
        return "masked" in r.stdout
    except Exception:
        return False


def make_status_row(icon: str = "emblem-ok-symbolic") -> Adw.ActionRow:
    row = Adw.ActionRow()
    row.set_title("Текущее состояние")
    row.set_icon_name(icon)
    row.set_subtitle("…")
    row.set_activatable(False)
    row.add_css_class("status-row")
    return row


def set_status_async(row: Adw.ActionRow, info_func):
    def done(res):
        row.set_subtitle(res if isinstance(res, str) else f"ошибка: {res}")
    bg(info_func, done)


def action_button(group, title: str, btn_label: str, callback, css="suggested-action", root=True):
    """callback(btn) вызывается по клику. root=True — кнопка блокируется на время root-задач."""
    row = Adw.ActionRow(title=title)
    btn = Gtk.Button(label=btn_label)
    if css:
        btn.add_css_class(css)
    btn.set_valign(Gtk.Align.CENTER)
    btn._pt_enabled = True
    btn.connect("clicked", lambda b: callback(b))
    row.add_suffix(btn)
    group.add(row)
    if root:
        _ACTION_BUTTONS.append(btn)
    return btn


def add_refresh(group, refresh_func):
    def cb(_btn):
        refresh_func()
        toast("Статус обновлён")
    action_button(group, "Перечитать live-статус", "↻ Обновить", cb, css=None, root=False)


def add_rollback(group, name, build_ops, refresh, ok_note=None):
    """Кнопка отката: предыдущая версия или исходная (до PixelTweak).
    build_ops(mode) вызывается ПОСЛЕ выбора — состояние системы читается в этот момент."""
    def click(btn):
        def on_mode(mode):
            built = build_ops(mode)
            if isinstance(built, tuple):
                ops, msg = built
            else:
                ops = built
                msg = ok_note or f"{name}: откат выполнен (перезапустите PixelTweak, чтобы обновить поля)"
            apply_job(btn, ops, msg, f"{name}: откат", after=lambda ok: refresh())

        ask(
            btn.get_root(), f"Откатить настройки «{name}»?",
            "«Предыдущая» — то, что было до последнего сохранения. «Исходная» — самая первая "
            "версия до PixelTweak (если файла раньше не было, он будет удалён).",
            [("last", "Предыдущую", False), ("orig", "Исходную", True)],
            on_mode,
        )
    return action_button(group, f"Откатить «{name}»", "↩ Откатить", click, css=None)


def read_ini(path: str, inline=None) -> configparser.ConfigParser:
    kw = {"inline_comment_prefixes": inline} if inline else {}
    cfg = configparser.ConfigParser(interpolation=None, strict=False,
                                    allow_no_value=True, **kw)
    try:
        cfg.read_string(read_file(path))
    except configparser.Error:
        pass
    return cfg


def ini_get(cfg, section: str, key: str, default: str) -> str:
    try:
        val = cfg.get(section, key, fallback=default)
    except configparser.Error:
        return default
    return val if val else default


def ini_set(text: str, section: str, values: dict) -> str:
    """Поменять только нужные ключи в секции, не трогая остальные секции/комментарии."""
    out, done = [], set()
    in_sec = found = False

    def flush():
        for k, v in values.items():
            if k not in done:
                out.append(f"{k} = {v}")
                done.add(k)

    for line in text.splitlines():
        hm = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if hm:
            if in_sec:
                flush()
            in_sec = hm.group(1).strip() == section
            found = found or in_sec
            out.append(line)
            continue
        if in_sec:
            km = re.match(r"^\s*([^#;=\s][^=]*?)\s*=", line)
            if km and km.group(1) in values and km.group(1) not in done:
                out.append(f"{km.group(1)} = {values[km.group(1)]}")
                done.add(km.group(1))
                continue
        out.append(line)
    if in_sec:
        flush()
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append(f"[{section}]")
        flush()
    return "\n".join(out) + "\n"


# ══════════════════════════════════════════════════════════════════════
#  1. ZRAM
# ══════════════════════════════════════════════════════════════════════
ZRAM_CONF = "/etc/systemd/zram-generator.conf"


def _meminfo(key: str) -> int:
    m = re.search(rf"^{key}:\s+(\d+)", read_file("/proc/meminfo"), re.M)
    return int(m.group(1)) if m else 0


def zram_swap_info():
    """(zram-swap включён?, занято KiB)."""
    for line in read_file("/proc/swaps").splitlines()[1:]:
        p = line.split()
        if len(p) >= 4 and "/zram" in p[0]:
            try:
                return True, int(p[3])
            except ValueError:
                return True, 0
    return False, 0


def _zram_fn1(f):
    def g(args):
        if len(args) != 1:
            raise ValueError("функция принимает один аргумент")
        return f(args[0])
    return g


_ZRAM_FUNCS = {
    "min": min, "max": max,
    "ceil": _zram_fn1(math.ceil), "int": _zram_fn1(math.trunc),
    "round": _zram_fn1(round), "abs": _zram_fn1(abs),
}
_ZRAM_TOK = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z_0-9]*)|(.))")


def eval_zram_size(expr: str, ram_mib: float) -> float:
    """Разбирает zram-size по грамматике zram-generator (ram, + - * / % ^, min/max/ceil/int/round/abs).
    Бросает ValueError с понятным текстом, если выражение некорректно. Результат — MiB."""
    s = expr.strip()
    if not s:
        raise ValueError("пусто")
    toks, pos = [], 0
    while pos < len(s):
        m = _ZRAM_TOK.match(s, pos)
        if not m:
            break
        num, ident, sym = m.groups()
        pos = m.end()
        if num is not None:
            toks.append(("num", float(num)))
        elif ident is not None:
            toks.append(("id", ident))
        elif sym is not None and sym in "+-*/%^(),":
            toks.append(("op", sym))
        else:
            raise ValueError(f"недопустимый символ «{sym}»")
    idx = 0

    def peek():
        return toks[idx] if idx < len(toks) else (None, None)

    def take():
        nonlocal idx
        t = toks[idx]
        idx += 1
        return t

    def expect(sym):
        if peek() != ("op", sym):
            raise ValueError(f"ожидалось «{sym}»")
        take()

    def expr_():
        v = term()
        while peek() in (("op", "+"), ("op", "-")):
            o = take()[1]
            r = term()
            v = v + r if o == "+" else v - r
        return v

    def term():
        v = unary()
        while peek() in (("op", "*"), ("op", "/"), ("op", "%")):
            o = take()[1]
            r = unary()
            if o == "*":
                v = v * r
            else:
                if r == 0:
                    raise ValueError("деление на ноль")
                v = v / r if o == "/" else math.fmod(v, r)
        return v

    def unary():
        if peek() == ("op", "-"):
            take()
            return -unary()
        return power()

    def power():
        base = atom()
        if peek() == ("op", "^"):
            take()
            return base ** unary()
        return base

    def atom():
        kind, val = peek()
        if kind is None:
            raise ValueError("выражение оборвано")
        take()
        if kind == "num":
            return val
        if kind == "op" and val == "(":
            v = expr_()
            expect(")")
            return v
        if kind == "id":
            if val == "ram":
                return float(ram_mib)
            if val == "e":
                return math.e
            if val == "pi":
                return math.pi
            if val in _ZRAM_FUNCS:
                expect("(")
                args = [expr_()]
                while peek() == ("op", ","):
                    take()
                    args.append(expr_())
                expect(")")
                return float(_ZRAM_FUNCS[val](args))
            raise ValueError(f"неизвестное имя «{val}»")
        raise ValueError(f"неожиданный символ «{val}»")

    result = expr_()
    if idx != len(toks):
        raise ValueError("лишние символы после выражения")
    return result


def zram_algos(current: str):
    """Алгоритмы из ядра (/sys/block/zram0/comp_algorithm); без zram0 — безопасный минимум."""
    raw = read_file("/sys/block/zram0/comp_algorithm", "").split()
    algos = [a.strip("[]") for a in raw]
    active = next((a.strip("[]") for a in raw if a.startswith("[")), "")
    if not algos:
        algos = ["lzo-rle", "lz4", "zstd", "lzo"]
    if current and current not in algos:
        algos.append(current)
    return algos, active


def zram_generator_installed() -> bool:
    paths = (
        "/usr/lib/systemd/system-generators/zram-generator",
        "/lib/systemd/system-generators/zram-generator",
        "/usr/lib/systemd/zram-generator",
    )
    return any(os.path.exists(p) for p in paths) or have("zram-generator")


def zram_live_info() -> str:
    gen = zram_generator_installed()
    active, used = zram_swap_info()
    if active:
        size_mb = 0
        for line in read_file("/proc/swaps").splitlines()[1:]:
            p = line.split()
            if len(p) >= 3 and "/zram" in p[0]:
                try:
                    size_mb = int(p[2]) // 1024
                except ValueError:
                    pass
                break
        _algos, algo = zram_algos("")
        s = f"Swap активен · {size_mb} MB · занято {used // 1024} MB · {algo or '?'}"
        if not gen:
            s += " · управляется не zram-generator (настройки ниже не повлияют)"
        return s
    if os.path.exists("/sys/block/zram0"):
        return "Устройство zram0 есть, но swap не включён"
    if not gen:
        return "ZRAM не активен · не установлен systemd-zram-generator"
    return "ZRAM не активен"


def page_zram() -> Adw.PreferencesPage:
    page = Adw.PreferencesPage(title="ZRAM", icon_name="drive-harddisk-symbolic")
    group = Adw.PreferencesGroup(title="ZRAM-генератор", description=ZRAM_CONF)

    status = make_status_row("drive-harddisk-symbolic")
    group.add(status)

    def refresh():
        set_status_async(status, zram_live_info)
    refresh()

    cfg = read_ini(ZRAM_CONF)
    size_val = ini_get(cfg, "zram0", "zram-size", "min(ram / 2, 4096)")
    algo_val = ini_get(cfg, "zram0", "compression-algorithm", "")

    size_row = Adw.EntryRow(title="Размер в MiB (например: ram / 2 или 4096)")
    size_row.set_text(size_val)
    group.add(size_row)

    algos, live_algo = zram_algos(algo_val)
    algo_row = Adw.ComboRow(title="Алгоритм сжатия (из ядра)")
    algo_row.set_model(Gtk.StringList.new(algos))
    pick = algo_val or live_algo or algos[0]
    if pick in algos:
        algo_row.set_selected(algos.index(pick))
    group.add(algo_row)

    def save(btn):
        if not zram_generator_installed():
            toast("Сначала: sudo apt install systemd-zram-generator")
            return
        size = size_row.get_text().strip()
        ram_mib = _meminfo("MemTotal") / 1024
        try:
            mib = eval_zram_size(size, ram_mib)
        except ValueError as e:
            toast(f"ZRAM: неверный размер — {e}")
            return
        if mib < 64 or mib > ram_mib * 4:
            toast(f"ZRAM: {int(mib)} MiB вне разумных пределов (64 MiB … 4×RAM)")
            return
        algo = algos[algo_row.get_selected()]
        text = ini_set(read_file(ZRAM_CONF), "zram0",
                       {"zram-size": size, "compression-algorithm": algo})
        write = W(ZRAM_CONF, text)
        reload_ = C(["systemctl", "daemon-reload"])
        active, used_kib = zram_swap_info()  # состояние на момент клика

        def save_only():
            apply_job(btn, [write, reload_],
                      "ZRAM: сохранено, применится после перезагрузки", "ZRAM",
                      after=lambda ok: refresh())

        def apply_now():
            act_now, _ = zram_swap_info()  # ещё раз — после диалога
            ops = [write, reload_]
            if act_now:
                ops.append(C(["systemctl", "stop", "dev-zram0.swap"], timeout=240))
            ops.append(C(["systemctl", "restart", "systemd-zram-setup@zram0.service"],
                         requires_prev=True, timeout=90))
            # включаем swap обратно в любом случае
            ops.append(C(["systemctl", "start", "dev-zram0.swap"], ignore=True))
            apply_job(btn, ops, f"ZRAM: применено ({int(mib)} MiB, {algo})", "ZRAM",
                      after=lambda ok: refresh())

        if not active:
            apply_now()
            return
        avail = _meminfo("MemAvailable")
        need = used_kib + 512 * 1024
        body = (f"Сейчас в ZRAM занято {used_kib // 1024} MB, свободно RAM ≈ {avail // 1024} MB. "
                "Для применения ZRAM-swap временно отключается: данные из него возвращаются в память.")
        options = [("save", "Только сохранить", False)]
        if avail >= need:
            body += " Памяти достаточно — можно применить сейчас."
            options.insert(0, ("now", "Применить сейчас", True))
        else:
            body += (f" Свободной RAM не хватает для безопасного перезапуска (нужно ≈ {need // 1024} MB), "
                     "иначе система может убить процессы. Настройка применится после перезагрузки.")
        ask(btn.get_root(), "Перезапустить ZRAM?", body, options,
            lambda rid: apply_now() if rid == "now" else save_only())

    def zram_rollback_ops(mode):
        act_now, used_kib = zram_swap_info()
        avail = _meminfo("MemAvailable")
        ops = [R(ZRAM_CONF, mode), C(["systemctl", "daemon-reload"])]
        if not act_now or avail >= used_kib + 512 * 1024:
            if act_now:
                ops.append(C(["systemctl", "stop", "dev-zram0.swap"], timeout=240))
            # без if_exists: если конфиг удалён, работает штатный из пакета; если его нет вовсе —
            # получим честную ошибку, а swap попробуем вернуть следующим шагом
            ops.append(C(["systemctl", "restart", "systemd-zram-setup@zram0.service"],
                         requires_prev=True, timeout=90))
            ops.append(C(["systemctl", "start", "dev-zram0.swap"], ignore=True))
            return ops, "ZRAM: откат выполнен и применён"
        return ops, "ZRAM: конфиг откачен, размер обновится после перезагрузки (мало свободной RAM)"

    action_button(group, "Применить настройки", "Сохранить", save)
    add_rollback(group, "ZRAM", zram_rollback_ops, refresh)
    add_refresh(group, refresh)
    page.add(group)
    return page


# ══════════════════════════════════════════════════════════════════════
#  2. sysctl
# ══════════════════════════════════════════════════════════════════════
SYSCTL_CONF = "/etc/sysctl.d/99-pixeltweak.conf"
SYSCTL_PARAMS = [
    ("vm.swappiness",             0, 200, 1, 60),
    ("vm.vfs_cache_pressure",     0, 500, 1, 100),
    ("vm.dirty_ratio",            0, 100, 1, 20),
    ("vm.dirty_background_ratio", 0, 100, 1, 10),
]


def _read_proc(key: str) -> str:
    return read_file(f"/proc/sys/{key.replace('.', '/')}", "").strip()


def sysctl_managed() -> dict:
    res = {}
    for line in read_file(SYSCTL_CONF).splitlines():
        m = re.match(r"^\s*(vm\.\w+)\s*=\s*(\d+)", line)
        if m:
            res[m.group(1)] = int(m.group(2))
    return res


def page_sysctl() -> Adw.PreferencesPage:
    page = Adw.PreferencesPage(title="Ядро", icon_name="applications-system-symbolic")
    group = Adw.PreferencesGroup(
        title="Параметры vm.*",
        description=f"{SYSCTL_CONF} · в файл попадают только изменённые параметры",
    )

    def runtime(key, dflt):
        try:
            return int(_read_proc(key) or dflt)
        except ValueError:
            return dflt

    managed0 = sysctl_managed()
    rows = {}
    for key, lo, hi, step, dflt in SYSCTL_PARAMS:
        current = runtime(key, dflt)
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(key)
        row.set_value(current)
        mark = " · задано PixelTweak" if key in managed0 else ""
        row.set_subtitle(f"в ядре сейчас: {current}{mark}")
        group.add(row)
        rows[key] = row

    def refresh():
        managed = sysctl_managed()
        for key, _lo, _hi, _st, dflt in SYSCTL_PARAMS:
            mark = " · задано PixelTweak" if key in managed else ""
            rows[key].set_subtitle(f"в ядре сейчас: {runtime(key, dflt)}{mark}")

    def save(btn):
        managed = sysctl_managed()
        now = {k: runtime(k, d) for k, _l, _h, _s, d in SYSCTL_PARAMS}
        vals = {}
        for key, row in rows.items():
            v = int(row.get_value())
            if key in managed or v != now[key]:
                vals[key] = v
        if not vals:
            toast("Ядро: нечего применять — значения совпадают с текущими")
            return
        eff_ratio = vals.get("vm.dirty_ratio", now["vm.dirty_ratio"])
        eff_bg = vals.get("vm.dirty_background_ratio", now["vm.dirty_background_ratio"])
        if eff_bg > eff_ratio:
            toast("Ядро: dirty_background_ratio не может быть больше dirty_ratio")
            return
        order = [k for k, *_ in SYSCTL_PARAMS if k in vals]
        if ("vm.dirty_ratio" in vals and "vm.dirty_background_ratio" in vals
                and vals["vm.dirty_ratio"] < now["vm.dirty_background_ratio"]):
            order.remove("vm.dirty_background_ratio")
            order.insert(order.index("vm.dirty_ratio"), "vm.dirty_background_ratio")
        lines = ["# Managed by PixelTweak"] + [f"{k} = {vals[k]}" for k in order]
        ops = [W(SYSCTL_CONF, "\n".join(lines) + "\n"),
               C(["sysctl", "-p", SYSCTL_CONF])]
        apply_job(btn, ops, f"Ядро: применено ({len(vals)} парам.)", "Ядро",
                  after=lambda ok: refresh())

    def rollback_ops(mode):
        d = {k: dflt for k, _l, _h, _s, dflt in SYSCTL_PARAMS}
        bgk, rk = "vm.dirty_background_ratio", "vm.dirty_ratio"
        ops = [R(SYSCTL_CONF, mode)]
        # Если файла больше нет — сначала значения ядра по умолчанию, затем sysctl --system
        # перечитает все остальные /etc/sysctl.d/* и перекроет их там, где они что-то задают.
        for key in ("vm.swappiness", "vm.vfs_cache_pressure"):
            ops.append(C(["sysctl", "-w", f"{key}={d[key]}"], if_missing=SYSCTL_CONF))
        ops.append(C(["sysctl", "-w", f"{rk}={d[rk]}"], if_missing=SYSCTL_CONF))
        ops.append(C(["sysctl", "-w", f"{bgk}={d[bgk]}"], if_missing=SYSCTL_CONF))
        # --system возвращает ненулевой код из-за посторонних конфигов — не считаем это провалом
        ops.append(C(["sysctl", "--system"], ignore=True))
        return ops

    action_button(group, "Применить параметры ядра", "Применить", save)
    add_rollback(group, "Ядро", rollback_ops, refresh)
    add_refresh(group, refresh)
    page.add(group)
    return page


# ══════════════════════════════════════════════════════════════════════
#  3. earlyoom
# ══════════════════════════════════════════════════════════════════════
EARLYOOM_CONF = "/etc/default/earlyoom"
DEFAULT_AVOID = r"(^|/)(chrome|firefox)$"


def earlyoom_live_info() -> str:
    if not have("earlyoom"):
        return "earlyoom не установлен (sudo apt install earlyoom)"
    if service_active("earlyoom.service"):
        pid = ""
        try:
            r = subprocess.run(
                ["systemctl", "show", "earlyoom.service", "-p", "MainPID", "--value"],
                capture_output=True, text=True, timeout=2,
            )
            pid = r.stdout.strip()
        except Exception:
            pass
        if pid and pid != "0":
            cmdline = read_file(f"/proc/{pid}/cmdline", "").replace("\x00", " ").strip()
            args_str = " ".join(cmdline.split()[1:]) if cmdline else ""
            return f"Служба активна · PID {pid}" + (f" · {args_str}" if args_str else "")
        return "Служба активна"
    return "Служба остановлена"


def parse_earlyoom(existing: str):
    """-m / -S возвращаем как «сырые» строки (например '5,2'), чтобы не терять хвост."""
    m = re.search(r'^\s*EARLYOOM_ARGS=(["\']?)(.*?)\1\s*$', existing, re.M)
    inner = m.group(2) if m else ""
    try:
        toks = shlex.split(inner)
    except ValueError:
        toks = inner.split()
    mem, swap, avoid, extra = "10", "10", None, []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-m", "-S", "--avoid") and i + 1 < len(toks):
            val = toks[i + 1]
            i += 2
            if t == "--avoid":
                avoid = val
            elif t == "-m":
                mem = val
            else:
                swap = val
            continue
        if t.startswith("--avoid="):
            avoid = t[len("--avoid="):]
            i += 1
            continue
        extra.append(t)
        i += 1
    return mem, swap, avoid, extra


def split_num(raw: str, default: int = 10):
    """'5,2' → (5, ',2')."""
    m = re.match(r"(\d+)(.*)$", raw)
    return (int(m.group(1)), m.group(2)) if m else (default, "")


def join_num(new: int, tail: str) -> str:
    """Возвращает исходный хвост; kill-порог не больше нового основного."""
    km = re.fullmatch(r",(\d+)", tail)
    if km:
        return f"{new},{min(int(km.group(1)), new)}"
    return f"{new}{tail}"


def page_earlyoom() -> Adw.PreferencesPage:
    page = Adw.PreferencesPage(title="EarlyOOM", icon_name="security-high-symbolic")
    group = Adw.PreferencesGroup(title="Защита от OOM", description=EARLYOOM_CONF)

    status = make_status_row("security-high-symbolic")
    group.add(status)

    def refresh():
        set_status_async(status, earlyoom_live_info)
    refresh()

    existing = read_file(EARLYOOM_CONF)
    m_raw, s_raw, avoid_now, _extra_args = parse_earlyoom(existing)
    m_now, m_tail = split_num(m_raw)
    s_now, s_tail = split_num(s_raw)

    mem_row = Adw.SpinRow.new_with_range(1, 50, 1)
    mem_row.set_title("Порог свободной памяти (%) -m")
    mem_row.set_value(m_now)
    mem_row.set_subtitle("если свободно меньше — убиваем процесс"
                         + (f" · сохраняется «{m_tail}»" if m_tail else ""))
    group.add(mem_row)

    swap_row = Adw.SpinRow.new_with_range(1, 50, 1)
    swap_row.set_title("Порог свободного swap (%) -S")
    swap_row.set_value(s_now)
    swap_row.set_subtitle("аналогично для swap"
                          + (f" · сохраняется «{s_tail}»" if s_tail else ""))
    group.add(swap_row)

    avoid_row = Adw.EntryRow(title="Не убивать (regex) --avoid")
    avoid_row.set_text(avoid_now if avoid_now is not None else DEFAULT_AVOID)
    group.add(avoid_row)

    def save(btn):
        if not have("earlyoom"):
            toast("Сначала: sudo apt install earlyoom")
            return
        avoid = avoid_row.get_text().strip()
        if re.search(r"""['"\\\n\r]""", avoid) or any(b in avoid for b in ("`", "$(", "${")):
            toast("--avoid: нельзя кавычки, обратный слэш, переводы строк и подстановки команд")
            return
        # файл и «хвосты» порогов читаем в момент клика
        cur = read_file(EARLYOOM_CONF)
        m_raw_cur, s_raw_cur, _av_cur, extra_cur = parse_earlyoom(cur)
        _, m_tail_cur = split_num(m_raw_cur)
        _, s_tail_cur = split_num(s_raw_cur)
        parts = ["-m", join_num(int(mem_row.get_value()), m_tail_cur),
                 "-S", join_num(int(swap_row.get_value()), s_tail_cur)]
        if avoid:
            parts += ["--avoid", avoid]
        parts += extra_cur
        args = " ".join(shlex.quote(p) for p in parts)
        if '"' in args:
            toast("earlyoom: в аргументах есть двойная кавычка, запись отменена")
            return
        new_line = f'EARLYOOM_ARGS="{args}"'
        # Оставляем только комментарии и единственную строку EARLYOOM_ARGS
        kept = [l for l in cur.splitlines() if not l.strip() or l.strip().startswith("#")]
        if not kept:
            kept = ["# Managed by PixelTweak"]
        text = "\n".join(kept + [new_line]) + "\n"
        ops = [W(EARLYOOM_CONF, text), C(["systemctl", "restart", "earlyoom.service"])]
        apply_job(btn, ops, "EarlyOOM: применено и перезапущено", "EarlyOOM",
                  after=lambda ok: refresh())

    action_button(group, "Применить настройки", "Применить", save)
    add_rollback(group, "EarlyOOM",
                 lambda mode: [R(EARLYOOM_CONF, mode),
                               C(["systemctl", "restart", "earlyoom.service"])],
                 refresh)
    add_refresh(group, refresh)
    page.add(group)
    return page


# ══════════════════════════════════════════════════════════════════════
#  4. gamemode
# ══════════════════════════════════════════════════════════════════════
GAMEMODE_CONF = "/etc/gamemode.ini"


def gamemode_live_info() -> str:
    if not have("gamemoded"):
        return "gamemode не установлен (sudo apt install gamemode)"
    state = "статус неизвестен"
    try:
        r = subprocess.run(["gamemoded", "-s"], capture_output=True, text=True, timeout=2)
        out = (r.stdout or r.stderr).strip()
        if "inactive" in out:
            state = "готов (ожидание игры)"
        elif "active" in out:
            state = "активен (игра запущена)"
        elif out:
            state = out[:80]
    except Exception:
        pass
    cfg_note = "конфиг найден" if os.path.exists(GAMEMODE_CONF) else "конфиг не создан (по умолчанию)"
    return f"Демон: {state} · {cfg_note}"


def page_gamemode() -> Adw.PreferencesPage:
    page = Adw.PreferencesPage(title="GameMode", icon_name="applications-games-symbolic")
    group = Adw.PreferencesGroup(
        title="Оптимизация для игр",
        description=f"{GAMEMODE_CONF} · записываются только изменённые параметры",
    )

    status = make_status_row("applications-games-symbolic")
    group.add(status)

    def refresh():
        set_status_async(status, gamemode_live_info)
    refresh()

    cfg = read_ini(GAMEMODE_CONF, inline=(";",))
    try:
        renice_raw = int(ini_get(cfg, "general", "renice", ""))
    except ValueError:
        renice_raw = None
    gov_raw = ini_get(cfg, "cpu", "desiredgov", "")
    soft_raw = ini_get(cfg, "general", "softrealtime", "")

    # renice в gamemode: 0…20, значение инвертируется (10 → nice -10), 0 = не менять.
    # Старые версии PixelTweak писали отрицательные числа — показываем модуль и поправим при сохранении.
    renice_init = abs(renice_raw) if renice_raw is not None else 0
    renice_row = Adw.SpinRow.new_with_range(0, 20, 1)
    renice_row.set_title("renice (0 = не менять)")
    renice_row.set_value(min(renice_init, 20))
    renice_row.set_subtitle("1–20: применяется как отрицательный nice (10 → −10); больше — выше приоритет")
    group.add(renice_row)

    avail = read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors").split()
    govs = avail or ["performance", "powersave", "schedutil"]
    gov_init = gov_raw or "performance"
    if gov_init not in govs:
        govs.append(gov_init)
    gov_row = Adw.ComboRow(title="CPU governor во время игры")
    gov_row.set_model(Gtk.StringList.new(govs))
    gov_row.set_selected(govs.index(gov_init))
    group.add(gov_row)

    soft_opts = ["auto", "on", "off"]
    soft_init = soft_raw if soft_raw in soft_opts else "auto"
    soft_row = Adw.ComboRow(title="softrealtime")
    soft_row.set_model(Gtk.StringList.new(soft_opts))
    soft_row.set_selected(soft_opts.index(soft_init))
    group.add(soft_row)

    def save(btn):
        # сравниваем с файлом в момент клика, а не с тем, что было при открытии окна
        cfg_cur = read_ini(GAMEMODE_CONF, inline=(";",))
        try:
            r_raw_cur = int(ini_get(cfg_cur, "general", "renice", ""))
        except ValueError:
            r_raw_cur = None
        r_cur = abs(r_raw_cur) if r_raw_cur is not None else 0
        gov_cur = ini_get(cfg_cur, "cpu", "desiredgov", "") or "performance"
        soft_cur = ini_get(cfg_cur, "general", "softrealtime", "")
        if soft_cur not in soft_opts:
            soft_cur = "auto"

        general, cpu = {}, {}
        renice = int(renice_row.get_value())
        if renice != r_cur or (r_raw_cur is not None and r_raw_cur < 0):
            general["renice"] = renice
        soft = soft_opts[soft_row.get_selected()]
        if soft != soft_cur:
            general["softrealtime"] = soft
        gov = govs[gov_row.get_selected()]
        if gov != gov_cur:
            cpu["desiredgov"] = gov
        if not general and not cpu and os.path.exists(GAMEMODE_CONF):
            toast("GameMode: изменений нет")
            return
        text = read_file(GAMEMODE_CONF)
        if not text.strip() and not general and not cpu:
            cpu["desiredgov"] = gov
        if general:
            text = ini_set(text, "general", general)
        if cpu:
            text = ini_set(text, "cpu", cpu)
        apply_job(btn, [W(GAMEMODE_CONF, text)], "GameMode: сохранено", "GameMode",
                  after=lambda ok: refresh())

    action_button(group, "Применить настройки", "Сохранить", save)
    add_rollback(group, "GameMode", lambda mode: [R(GAMEMODE_CONF, mode)], refresh)
    add_refresh(group, refresh)
    page.add(group)
    return page


# ══════════════════════════════════════════════════════════════════════
#  5. CPU (через sysfs, без cpupower)
# ══════════════════════════════════════════════════════════════════════
CPU_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq"
PPD_UNIT = "power-profiles-daemon.service"


def _cpu_dirs() -> list:
    return sorted(glob.glob(CPU_GLOB))


def _int_file(path: str) -> int:
    try:
        return int(read_file(path, "").strip())
    except ValueError:
        return 0


def _cpu_limits_mhz():
    """(мин, макс) текущих scaling-лимитов по всем ядрам, МГц."""
    dirs = _cpu_dirs()
    mins = [x for x in (_int_file(f"{d}/scaling_min_freq") // 1000 for d in dirs) if x]
    maxs = [x for x in (_int_file(f"{d}/scaling_max_freq") // 1000 for d in dirs) if x]
    return (min(mins) if mins else 0), (max(maxs) if maxs else 0)


def _cpu_hw_khz():
    """Аппаратный диапазон (объединение по всем ядрам — важно для P/E-ядер), кГц."""
    dirs = _cpu_dirs()
    los = [x for x in (_int_file(f"{d}/cpuinfo_min_freq") for d in dirs) if x]
    his = [x for x in (_int_file(f"{d}/cpuinfo_max_freq") for d in dirs) if x]
    return (min(los) if los else 0), (max(his) if his else 0)


def _cpu_governors_now() -> set:
    return {read_file(f"{d}/scaling_governor", "").strip() for d in _cpu_dirs()} - {""}


def cpupower_live_info() -> str:
    dirs = _cpu_dirs()
    if not dirs:
        return "cpufreq недоступен (виртуалка или драйвер без cpufreq)"
    govs = _cpu_governors_now() or {"?"}
    gov = next(iter(govs)) if len(govs) == 1 else "разные: " + ", ".join(sorted(govs))
    mn, mx = _cpu_limits_mhz()
    curs = [_int_file(f"{d}/scaling_cur_freq") // 1000 for d in dirs]
    cur = max(curs) if curs else 0
    s = f"governor: {gov}"
    if mn and mx:
        s += f" · {mn}–{mx} MHz"
    if cur:
        s += f" · сейчас до {cur} MHz"
    if os.path.exists(CPU_CONF):
        s += " · политика PixelTweak включена"
    return s


def cpu_conf_text(gov, lo_khz, hi_khz, header="# Managed by PixelTweak") -> str:
    lines = [header]
    if gov:
        lines.append(f"GOVERNOR={gov}")
    lines += [f"MIN_KHZ={lo_khz}", f"MAX_KHZ={hi_khz}"]
    return "\n".join(lines) + "\n"


CPU_UNIT_TEXT = (
    "[Unit]\n"
    "Description=PixelTweak CPU frequency settings\n"
    "After=power-profiles-daemon.service tuned.service\n\n"
    "[Service]\n"
    "Type=oneshot\n"
    "RemainAfterExit=yes\n"
    f"ExecStart={CPU_APPLY_PATH}\n\n"
    "[Install]\n"
    "WantedBy=multi-user.target\n"
)


def ppd_installed() -> bool:
    return any(os.path.exists(p) for p in (
        "/usr/lib/systemd/system/power-profiles-daemon.service",
        "/lib/systemd/system/power-profiles-daemon.service",
    ))


def page_cpupower() -> Adw.PreferencesPage:
    page = Adw.PreferencesPage(title="CPU", icon_name="cpu-symbolic")
    group = Adw.PreferencesGroup(
        title="Управление частотой CPU",
        description="Запись напрямую в sysfs (cpupower не нужен) · сохраняется после перезагрузки",
    )

    status = make_status_row("cpu-symbolic")
    group.add(status)

    dirs = _cpu_dirs()
    available = bool(dirs)
    base = dirs[0] if dirs else None

    govs_str = read_file(f"{base}/scaling_available_governors").strip() if base else ""
    govs = govs_str.split() if govs_str else ["performance", "powersave", "schedutil"]
    gnow = _cpu_governors_now()
    current_gov = sorted(gnow)[0] if gnow else ""

    hw_lo_khz, hw_hi_khz = _cpu_hw_khz()
    hw_lo, hw_hi = hw_lo_khz // 1000, hw_hi_khz // 1000
    if not hw_lo or not hw_hi or hw_hi <= hw_lo:
        hw_lo, hw_hi = 400, 6000

    gov_row = Adw.ComboRow(title="Губернатор частоты")
    gov_row.set_model(Gtk.StringList.new(govs))
    if current_gov in govs:
        gov_row.set_selected(govs.index(current_gov))
    gov_row.set_subtitle(f"сейчас: {current_gov or '?'}")
    group.add(gov_row)

    min_now, max_now = _cpu_limits_mhz()
    range_note = f"допустимо {hw_lo}–{hw_hi} (для каждого ядра — по его пределам)"

    min_row = Adw.SpinRow.new_with_range(hw_lo, hw_hi, 100)
    min_row.set_title("Мин. частота (МГц)")
    min_row.set_value(min_now or hw_lo)
    min_row.set_subtitle(f"сейчас: {min_now or '?'} МГц · {range_note}")
    group.add(min_row)

    max_row = Adw.SpinRow.new_with_range(hw_lo, hw_hi, 100)
    max_row.set_title("Макс. частота (МГц)")
    max_row.set_value(max_now or hw_hi)
    max_row.set_subtitle(f"сейчас: {max_now or '?'} МГц · {range_note}")
    group.add(max_row)

    # ── конфликт с power-profiles-daemon (через mask/unmask) ─────────
    ppd = {"enabled": None}
    ppd_btn = None
    ppd_row = None

    def refresh_ppd():
        if ppd_btn is None:
            return

        def check():
            return service_active(PPD_UNIT), service_masked(PPD_UNIT)

        def done(res):
            act, masked = res if isinstance(res, tuple) else (False, False)
            ppd["enabled"] = act or not masked
            if act:
                ppd_row.set_subtitle(
                    "активен: может перезаписывать governor (наш сервис запускается после него, "
                    "но смена профиля в GNOME снова всё перетрёт)")
            elif masked:
                ppd_row.set_subtitle("замаскирован (mask): GNOME не сможет запустить службу по D-Bus")
            else:
                ppd_row.set_subtitle("остановлен, но не замаскирован (GNOME может запустить по D-Bus)")
            ppd_btn.set_label("Отключить" if ppd["enabled"] else "Включить")
            btn_enable(ppd_btn, True)
        bg(check, done)

    def refresh():
        set_status_async(status, cpupower_live_info)
        d = _cpu_dirs()
        if d:
            g = _cpu_governors_now()
            gov_row.set_subtitle(f"сейчас: {', '.join(sorted(g)) or '?'}")
            mn, mx = _cpu_limits_mhz()
            min_row.set_subtitle(f"сейчас: {mn or '?'} МГц · {range_note}")
            max_row.set_subtitle(f"сейчас: {mx or '?'} МГц · {range_note}")
        refresh_ppd()

    def apply(btn):
        # всё «текущее» читаем в момент клика — иначе в cpu.orig.conf попадут устаревшие значения
        cur_dirs = _cpu_dirs()
        cur_hw_lo_khz, cur_hw_hi_khz = _cpu_hw_khz()
        if not cur_dirs or not cur_hw_lo_khz or not cur_hw_hi_khz:
            toast("CPU: cpufreq недоступен в этой системе")
            return
        lo = int(min_row.get_value())
        hi = int(max_row.get_value())
        if lo >= hi:
            toast("CPU: минимум должен быть меньше максимума")
            return
        gov = govs[gov_row.get_selected()]
        if not re.fullmatch(r"[a-z_]+", gov):
            toast("CPU: странное имя governor'а")
            return
        cur_gnow = _cpu_governors_now()
        cur_min_mhz, cur_max_mhz = _cpu_limits_mhz()
        orig_gov = next(iter(cur_gnow)) if len(cur_gnow) == 1 else ""
        orig_lo_khz = cur_min_mhz * 1000 if cur_min_mhz else cur_hw_lo_khz
        orig_hi_khz = cur_max_mhz * 1000 if cur_max_mhz else cur_hw_hi_khz
        ops = [
            W(CPU_ORIG, cpu_conf_text(orig_gov, orig_lo_khz, orig_hi_khz,
                                      "# PixelTweak: исходные значения CPU"),
              only_if_missing=True),
            W(CPU_CONF, cpu_conf_text(gov, lo * 1000, hi * 1000)),
            W(CPU_UNIT, CPU_UNIT_TEXT),
            C([CPU_APPLY_PATH]),
            C(["systemctl", "daemon-reload"]),
            C(["systemctl", "enable", "pixeltweak-cpu.service"]),
        ]
        apply_job(btn, ops, f"CPU: {gov}, {lo}–{hi} МГц (сохранено и после перезагрузки)",
                  "CPU", after=lambda ok: refresh())

    def reset(btn):
        def go(_rid):
            ops = [
                C([CPU_APPLY_PATH, CPU_ORIG], if_exists=CPU_ORIG),
                # нет сохранённых исходных значений — реальный сброс к аппаратным пределам
                C([CPU_APPLY_PATH, "--reset"], if_missing=CPU_ORIG),
                C(["systemctl", "disable", "--now", "pixeltweak-cpu.service"], ignore=True),
                D(CPU_UNIT),
                D(CPU_CONF),
                D(CPU_ORIG),
                C(["systemctl", "daemon-reload"]),
            ]
            apply_job(btn, ops, "CPU: сброшено, сервис автозагрузки удалён", "CPU: сброс",
                      after=lambda ok: refresh())
        ask(btn.get_root(), "Сбросить настройки CPU?",
            "Governor и пределы частоты вернутся к значениям, которые были до первого применения "
            "(если исходные не сохранены — к аппаратным пределам и стандартному governor'у). "
            "Сервис автозагрузки будет удалён.",
            [("reset", "Сбросить", True)], go)

    apply_btn = action_button(group, "Применить к CPU", "Применить", apply)
    btn_enable(apply_btn, available)
    action_button(group, "Сбросить к исходным и удалить сервис", "↩ Сбросить", reset, css=None)

    if ppd_installed():
        ppd_row = Adw.ActionRow(title="power-profiles-daemon")
        ppd_row.set_subtitle("…")
        ppd_btn = Gtk.Button(label="…")
        ppd_btn.set_valign(Gtk.Align.CENTER)
        ppd_btn._pt_enabled = False
        ppd_btn.set_sensitive(False)
        _ACTION_BUTTONS.append(ppd_btn)

        def ppd_click(btn):
            disable = ppd["enabled"]

            def go(_rid):
                if disable:
                    ops = [
                        C(["systemctl", "disable", "--now", PPD_UNIT], ignore=True),
                        C(["systemctl", "mask", PPD_UNIT]),
                    ]
                    ok_msg = "power-profiles-daemon отключён и замаскирован (mask)"
                else:
                    ops = [
                        C(["systemctl", "unmask", PPD_UNIT]),
                        C(["systemctl", "enable", "--now", PPD_UNIT]),
                    ]
                    ok_msg = "power-profiles-daemon размаскирован и включён"
                apply_job(btn, ops, ok_msg, "power-profiles-daemon",
                          after=lambda ok: refresh_ppd())
            if disable:
                ask(btn.get_root(), "Отключить и замаскировать power-profiles-daemon?",
                    "Служба будет остановлена и замаскирована (systemctl mask), чтобы GNOME "
                    "не мог перезапустить её через D-Bus и сбросить governor. "
                    "Переключатель профилей питания в настройках GNOME перестанет работать. "
                    "Включить обратно можно этой же кнопкой.",
                    [("off", "Отключить", True)], go)
            else:
                go("on")
        ppd_btn.connect("clicked", lambda b: ppd_click(b))
        ppd_row.add_suffix(ppd_btn)
        group.add(ppd_row)

    refresh()
    add_refresh(group, refresh)
    page.add(group)
    return page


# ══════════════════════════════════════════════════════════════════════
#  6. cacheclean
# ══════════════════════════════════════════════════════════════════════
CACHECLEAN_SVC   = "/etc/systemd/system/cacheclean.service"
CACHECLEAN_TIMER = "/etc/systemd/system/cacheclean.timer"

SVC_CONTENT = f"""[Unit]
Description=Clean page cache, dentries and inodes

[Service]
Type=oneshot
ExecStart={CACHECLEAN_SCRIPT}
"""

TIMER_TPL = """[Unit]
Description=Periodic cache cleaning

[Timer]
OnBootSec=5min
OnUnitActiveSec={interval}
Unit=cacheclean.service

[Install]
WantedBy=timers.target
"""


def _busctl_u64(unit: str, iface: str, prop: str) -> int:
    path = "/org/freedesktop/systemd1/unit/" + unit.replace(".", "_2e")
    r = subprocess.run(
        ["busctl", "--system", "get-property", "org.freedesktop.systemd1",
         path, iface, prop],
        capture_output=True, text=True, timeout=2,
    )
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) < 2:
        return 0
    return int(parts[1])


def next_timer_run(unit: str) -> str:
    """Следующий запуск таймера (и OnCalendar, и OnBootSec/OnUnitActiveSec)."""
    import time
    from datetime import timedelta
    try:
        iface = "org.freedesktop.systemd1.Timer"
        real = _busctl_u64(unit, iface, "NextElapseUSecRealtime")
        if real > 0:
            return datetime.fromtimestamp(real / 1e6).strftime("%Y-%m-%d %H:%M")
        mono = _busctl_u64(unit, iface, "NextElapseUSecMonotonic")
        if mono > 0:
            delta = mono / 1e6 - time.monotonic()
            if delta <= 0:
                return "сейчас"
            return (datetime.now() + timedelta(seconds=delta)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return ""


def cacheclean_live_info() -> str:
    if not os.path.exists(CACHECLEAN_TIMER):
        return "Таймер ещё не создан"
    if not service_active("cacheclean.timer"):
        return "Таймер неактивен"
    nxt = next_timer_run("cacheclean.timer")
    if nxt:
        return f"Активен · следующий запуск: {nxt}"
    return "Активен · время следующего запуска неизвестно"


def _read_current_interval() -> str:
    m = re.search(r"OnUnitActiveSec=(\S+)", read_file(CACHECLEAN_TIMER))
    return m.group(1) if m else "6h"


def page_cacheclean() -> Adw.PreferencesPage:
    page = Adw.PreferencesPage(title="Cache", icon_name="user-trash-symbolic")
    group = Adw.PreferencesGroup(
        title="Очистка кэша",
        description=f"Скрипт: {CACHECLEAN_SCRIPT} · сброс кэша обычно не ускоряет систему",
    )

    status = make_status_row("user-trash-symbolic")
    group.add(status)

    intervals = {
        "Каждые 15 минут": "15min",
        "Каждый час":      "1h",
        "Каждые 3 часа":   "3h",
        "Каждые 6 часов":  "6h",
        "Каждые 12 часов": "12h",
        "Раз в сутки":     "24h",
    }
    labels = list(intervals.keys())
    current = _read_current_interval()

    enable_row = Adw.SwitchRow(title="Включить таймер")
    enable_row.set_subtitle("сейчас: …")
    group.add(enable_row)

    interval_row = Adw.ComboRow(title="Интервал")
    interval_row.set_model(Gtk.StringList.new(labels))
    if current in intervals.values():
        interval_row.set_selected(list(intervals.values()).index(current))
    else:
        interval_row.set_selected(3)
    interval_row.set_subtitle(f"сейчас: {labels[interval_row.get_selected()]}")
    group.add(interval_row)

    def sync_enable():
        """Состояние таймера читаем в фоне (systemctl может тормозить)."""
        def done(res):
            active = res is True
            enable_row.set_active(active)
            enable_row.set_subtitle(f"сейчас: {'активен' if active else 'выключен'}")
        bg(lambda: service_active("cacheclean.timer"), done)

    def refresh():
        set_status_async(status, cacheclean_live_info)
    refresh()
    sync_enable()

    def save(btn):
        interval = intervals[labels[interval_row.get_selected()]]
        enable = enable_row.get_active()
        ops = [
            W(CACHECLEAN_SVC, SVC_CONTENT),
            W(CACHECLEAN_TIMER, TIMER_TPL.format(interval=interval)),
            C(["systemctl", "daemon-reload"]),
            C(["systemctl", "enable" if enable else "disable", "--now", "cacheclean.timer"]),
        ]

        def after(ok):
            if ok:
                interval_row.set_subtitle(f"сейчас: {labels[interval_row.get_selected()]}")
            refresh()
            sync_enable()

        ok_text = f"Таймер включён ({interval})" if enable else "Таймер выключен"
        apply_job(btn, ops, ok_text, "cacheclean", after=after)

    def clean_now(btn):
        apply_job(btn, [C([CACHECLEAN_SCRIPT])], "Кэш очищен", "Очистка кэша")

    def remove_timer(btn):
        def go(_rid):
            ops = [
                C(["systemctl", "disable", "--now", "cacheclean.timer"], ignore=True),
                D(CACHECLEAN_TIMER),
                D(CACHECLEAN_SVC),
                C(["systemctl", "daemon-reload"]),
            ]

            def after(ok):
                refresh()
                sync_enable()
            apply_job(btn, ops, "Таймер и сервис очистки удалены", "cacheclean: удаление", after=after)
        ask(btn.get_root(), "Удалить таймер очистки кэша?",
            "Таймер отключится, файлы cacheclean.timer и cacheclean.service будут удалены "
            "(копии останутся в бэкапах).", [("rm", "Удалить", True)], go)

    def refresh_all():
        refresh()
        sync_enable()

    action_button(group, "Сохранить настройки таймера", "Сохранить", save)
    action_button(group, "Очистить кэш сейчас", "Очистить", clean_now, css="destructive-action")
    action_button(group, "Удалить таймер и сервис", "✖ Удалить", remove_timer, css=None)
    add_refresh(group, refresh_all)

    page.add(group)
    return page


# ══════════════════════════════════════════════════════════════════════
#  Приложение
# ══════════════════════════════════════════════════════════════════════
def kill_popover_arrows(widget):
    """Убираем стрелку у всех поповеров штатным способом GTK (set_has_arrow)."""
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Popover):
            child.set_has_arrow(False)
        kill_popover_arrows(child)
        child = child.get_next_sibling()


class PixelTweakApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        global _toast_overlay, _cancel_btn

        # повторный запуск — просто показываем уже открытое окно
        win = self.props.active_window
        if win is not None:
            win.present()
            return

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        sm = Adw.StyleManager.get_default()
        sm.set_color_scheme(Adw.ColorScheme.PREFER_DARK)

        win = Adw.ApplicationWindow(application=app)
        win.set_title(APP_NAME)
        win.set_default_size(820, 760)

        log_action = Gio.SimpleAction.new("show-log", None)
        log_action.connect("activate", lambda *_a: show_log(win))
        win.add_action(log_action)

        view_stack = Gtk.Stack()
        view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        tabs = [
            (page_zram(),       "zram",     "💾 ZRAM"),
            (page_sysctl(),     "sysctl",   "⚙️ Ядро"),
            (page_earlyoom(),   "earlyoom", "🛡️ EarlyOOM"),
            (page_gamemode(),   "gamemode", "🎮 GameMode"),
            (page_cpupower(),   "cpu",      "🧠 CPU"),
            (page_cacheclean(), "cache",    "🧹 Cache"),
        ]
        for page, name, title in tabs:
            view_stack.add_titled(page, name, title)

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(view_stack)

        # В узком окне вкладки прокручиваются, а не вылезают за экран
        switch_scroll = Gtk.ScrolledWindow()
        switch_scroll.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        switch_scroll.set_propagate_natural_width(True)
        switch_scroll.set_min_content_width(240)
        switch_scroll.set_child(switcher)

        header = Adw.HeaderBar()
        header.set_title_widget(switch_scroll)
        log_btn = Gtk.Button(label="Журнал")
        log_btn.connect("clicked", lambda _b: show_log(win))
        header.pack_start(log_btn)

        # Кнопка отмены появляется только пока выполняется root-задача
        _cancel_btn = Gtk.Button(label="Отменить")
        _cancel_btn.add_css_class("destructive-action")
        _cancel_btn.set_visible(False)
        _cancel_btn.connect("clicked", cancel_current_job)
        header.pack_start(_cancel_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(view_stack)

        _toast_overlay = Adw.ToastOverlay()
        _toast_overlay.set_child(toolbar)
        win.set_content(_toast_overlay)

        def fix_arrows(*_a):
            GLib.idle_add(lambda: (kill_popover_arrows(win), False)[1])
        win.connect("map", fix_arrows)
        view_stack.connect("notify::visible-child", fix_arrows)

        win.present()


if __name__ == "__main__":
    if "--uninstall-helper" in sys.argv:
        if os.path.exists(HELPER_PATH):
            res = run_root([{"op": "uninstall_helper"}])
            ok, msg = summarize(res)
            print("Помощник удалён." if ok else f"Ошибка: {msg}")
            sys.exit(0 if ok else 1)
        print("Помощник не установлен.")
        sys.exit(0)
    app = PixelTweakApp()
    sys.exit(app.run(sys.argv))
