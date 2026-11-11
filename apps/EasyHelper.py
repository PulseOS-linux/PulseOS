#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyHelper — помощник новичка в PulseOS."""
import ast
import glob
import hashlib
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Vte", "3.91")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk, Pango, Vte  # noqa: E402

APP_ID = "org.easyhelper.App"
APP_DIR = Path(GLib.get_user_data_dir()) / "easyhelper"
CONTAINER = "easyhelper-sandbox"
LOGO_PATH = "/usr/share/ksnip_20261111-174102-removebg-preview.png"

# ════════════════════════════════════════════════════════════════════
#  СТИЛИ
#  Окно прозрачное "в целом", но все части интерфейса, кроме терминала,
#  закрашены непрозрачным цветом (.opaque-bg). Поэтому прозрачным
#  остаётся только терминал.
# ════════════════════════════════════════════════════════════════════
CSS = """
window.background { background-color: transparent; color:#e8e3f3; }
window.background.opaque-bg { background-color:#181326; }
.opaque-bg { background-color:#181326; }
.pad-bar { padding:8px 14px; }
headerbar { background:#1e182f; box-shadow:none; border-bottom:1px solid #332a4d; }
stackswitcher button {
  background:transparent; border:1px solid #3a3058; border-radius:4px;
  padding:6px 18px; margin:0 2px; color:#e8e3f3; box-shadow:none; background-image:none; }
stackswitcher button:checked { background:#3b2d63; border-color:#a855f7; }
.app-title { font-size:26px; font-weight:800; }
.app-sub   { color:#a29bb8; }
.section-title { font-weight:700; font-size:15px; color:#d8b4fe; margin-top:6px; }
.dim { color:#8f88a6; font-size:12px; }
list.card-list { background:#2a2142; border-radius:6px; }
list.card-list > row { background:transparent; padding:10px 14px; border-bottom:1px solid #3a3058; }
list.card-list > row:last-child { border-bottom:none; }
.keycap { background:#3f3162; border-radius:5px; padding:2px 9px; font-family:monospace;
          font-weight:600; border-bottom:2px solid #1b1430; color:#f3e8ff; }
.cmd { font-family:monospace; font-size:13px; color:#e9d5ff; font-weight:600; }
button.accent-btn { background:#a855f7; background-image:none; color:#fff; font-weight:600;
          border-radius:4px; padding:6px 14px; border:none; box-shadow:none; }
button.accent-btn:hover { background:#b66cfa; }
button.ghost-btn { background:transparent; background-image:none; border:1px solid #4a3d6e;
          border-radius:4px; padding:6px 14px; color:#e8e3f3; box-shadow:none; }
button.ghost-btn:hover { background:#34285a; }
button.danger-btn { background:#dc2626; background-image:none; color:#fff; border:none;
          border-radius:4px; padding:6px 14px; box-shadow:none; }
entry, searchentry { background:#2a2142; color:#e8e3f3; border:1px solid #3a3058;
          border-radius:5px; box-shadow:none; }
entry.cmd-entry { font-family:monospace; font-size:14px; }
.warn { color:#fca5a5; }
.ok   { color:#86efac; }
.terminal-frame { border-top:1px solid #3a3058; border-bottom:1px solid #3a3058; }
scrolledwindow, viewport { background:transparent; }
"""

# ════════════════════════════════════════════════════════════════════
#  КОМАНДЫ  (категория, команда, описание, опасная?)
#  <в угловых скобках> — место, куда надо подставить своё значение
# ════════════════════════════════════════════════════════════════════
COMMANDS = [
    # ── APT
    ("Обновление системы (APT)", "sudo apt update",
     "Обновляет СПИСОК доступных пакетов из репозиториев. Сами программы не меняет — это «посмотреть, что нового есть».", False),
    ("Обновление системы (APT)", "sudo apt upgrade",
     "Ставит новые версии уже установленных программ. Ничего не удаляет и не добавляет — безопасное обновление.", False),
    ("Обновление системы (APT)", "sudo apt full-upgrade",
     "Полное обновление: может удалять старые и ставить новые пакеты, если это нужно для обновления (например, новое ядро).", False),
    ("Обновление системы (APT)", "sudo apt dist-upgrade",
     "Старое название full-upgrade. Умно разрешает зависимости, обновляя систему целиком.", False),
    ("Обновление системы (APT)", "sudo apt install <пакет>",
     "Устанавливает программу по имени пакета. Например: sudo apt install vlc", False),
    ("Обновление системы (APT)", "sudo apt remove <пакет>",
     "Удаляет программу, но оставляет её файлы настроек.", False),
    ("Обновление системы (APT)", "sudo apt purge <пакет>",
     "Удаляет программу ВМЕСТЕ с настройками.", False),
    ("Обновление системы (APT)", "sudo apt autoremove",
     "Удаляет «осиротевшие» пакеты, которые ставились как зависимости и больше не нужны.", False),
    ("Обновление системы (APT)", "sudo apt autoclean",
     "Чистит кэш скачанных .deb-файлов, которые уже устарели.", False),
    ("Обновление системы (APT)", "sudo apt clean",
     "Полностью очищает кэш скачанных пакетов — освобождает место на диске.", False),
    ("Обновление системы (APT)", "apt search <слово>",
     "Ищет пакеты по слову в названии и описании.", False),
    ("Обновление системы (APT)", "apt show <пакет>",
     "Показывает подробную информацию о пакете: версию, размер, описание.", False),
    ("Обновление системы (APT)", "apt list --installed",
     "Список всех установленных пакетов.", False),
    ("Обновление системы (APT)", "apt list --upgradable",
     "Какие пакеты можно обновить (после apt update).", False),
    ("Обновление системы (APT)", "sudo apt --fix-broken install",
     "Пытается починить сломанные зависимости после неудачной установки.", False),
    # ── Пакеты
    ("Snap / Flatpak / dpkg", "snap list", "Список программ, установленных через Snap.", False),
    ("Snap / Flatpak / dpkg", "sudo snap install <пакет>", "Установить программу из Snap Store.", False),
    ("Snap / Flatpak / dpkg", "sudo snap refresh", "Обновить все snap-программы.", False),
    ("Snap / Flatpak / dpkg", "flatpak list", "Список Flatpak-программ.", False),
    ("Snap / Flatpak / dpkg", "flatpak update", "Обновить все Flatpak-программы.", False),
    ("Snap / Flatpak / dpkg", "sudo dpkg -i <файл.deb>", "Установить скачанный .deb-файл вручную.", False),
    ("Snap / Flatpak / dpkg", "dpkg -l | grep <имя>", "Проверить, установлен ли пакет с таким именем.", False),
    # ── Файлы
    ("Файлы и папки", "pwd", "Показать, в какой папке вы сейчас находитесь.", False),
    ("Файлы и папки", "ls -la", "Список файлов (включая скрытые) с правами, размером и датой.", False),
    ("Файлы и папки", "cd <папка>", "Перейти в папку. «cd ..» — на уровень вверх, «cd ~» — домой.", False),
    ("Файлы и папки", "mkdir -p <папка>", "Создать папку (и все родительские, если их нет).", False),
    ("Файлы и папки", "touch <файл>", "Создать пустой файл или обновить время изменения.", False),
    ("Файлы и папки", "cp -r <откуда> <куда>", "Копировать файл или папку (-r — рекурсивно).", False),
    ("Файлы и папки", "mv <откуда> <куда>", "Переместить или переименовать файл/папку.", False),
    ("Файлы и папки", "rm <файл>", "Удалить файл. В корзину НЕ попадает — удаляется навсегда!", True),
    ("Файлы и папки", "rm -r <папка>", "Удалить папку со всем содержимым. Навсегда! Проверяйте путь дважды.", True),
    ("Файлы и папки", "cat <файл>", "Вывести содержимое файла в терминал.", False),
    ("Файлы и папки", "less <файл>", "Постранично читать файл (q — выход, / — поиск).", False),
    ("Файлы и папки", "tail -f <файл>", "Следить за концом файла в реальном времени (удобно для логов).", False),
    ("Файлы и папки", "nano <файл>", "Простой текстовый редактор в терминале (Ctrl+O — сохранить, Ctrl+X — выход).", False),
    ("Файлы и папки", 'find ~ -name "<имя>"', "Найти файл по имени в домашней папке.", False),
    ("Файлы и папки", 'grep -rn "<текст>" .', "Найти текст внутри файлов текущей папки.", False),
    ("Файлы и папки", "du -sh *", "Размер каждого файла и папки в текущей директории.", False),
    ("Файлы и папки", "chmod +x <файл>", "Разрешить запуск файла как программы.", False),
    ("Файлы и папки", "sudo chown $USER:$USER <файл>", "Сделать себя владельцем файла.", False),
    ("Файлы и папки", "tar -czf <архив.tar.gz> <папка>", "Упаковать папку в архив tar.gz.", False),
    ("Файлы и папки", "tar -xzf <архив.tar.gz>", "Распаковать архив tar.gz.", False),
    ("Файлы и папки", "ln -s <цель> <ссылка>", "Создать символическую ссылку (как ярлык).", False),
    # ── Система
    ("Информация о системе", "uname -a", "Версия ядра и архитектура.", False),
    ("Информация о системе", "lsb_release -a", "Какая версия системы установлена.", False),
    ("Информация о системе", "hostnamectl", "Имя компьютера, ОС, ядро, виртуализация.", False),
    ("Информация о системе", "uptime", "Сколько времени система работает и средняя нагрузка.", False),
    ("Информация о системе", "whoami", "Под каким пользователем вы работаете.", False),
    ("Информация о системе", "df -h", "Свободное и занятое место на дисках.", False),
    ("Информация о системе", "free -h", "Использование оперативной памяти и swap.", False),
    ("Информация о системе", "lscpu", "Информация о процессоре.", False),
    ("Информация о системе", "lsblk", "Дерево дисков и разделов.", False),
    ("Информация о системе", "lspci", "Список PCI-устройств (видеокарта, сеть и т.д.).", False),
    ("Информация о системе", "lsusb", "Список подключённых USB-устройств.", False),
    ("Информация о системе", "ip a", "Сетевые интерфейсы и IP-адреса.", False),
    ("Информация о системе", "ping -c 4 8.8.8.8", "Проверить связь с интернетом (4 пакета).", False),
    ("Информация о системе", "ss -tulpn", "Какие программы слушают какие сетевые порты.", False),
    # ── Процессы и службы
    ("Процессы и службы", "top", "Монитор процессов (q — выход).", False),
    ("Процессы и службы", "htop", "Удобный монитор процессов (sudo apt install htop).", False),
    ("Процессы и службы", "ps aux | grep <имя>", "Найти запущенный процесс по имени.", False),
    ("Процессы и службы", "kill <PID>", "Завершить процесс по номеру. kill -9 — принудительно.", False),
    ("Процессы и службы", "killall <имя>", "Завершить все процессы с таким именем.", False),
    ("Процессы и службы", "systemctl status <сервис>", "Статус службы (например: ssh, NetworkManager).", False),
    ("Процессы и службы", "sudo systemctl restart <сервис>", "Перезапустить службу.", False),
    ("Процессы и службы", "systemctl --failed", "Показать службы, которые не смогли запуститься.", False),
    ("Процессы и службы", "journalctl -xe", "Последние системные сообщения с пояснениями (для поиска ошибок).", False),
    ("Процессы и службы", "journalctl -b -p err", "Только ошибки с момента последней загрузки.", False),
    ("Процессы и службы", "dmesg | tail", "Последние сообщения ядра (драйверы, USB, диски).", False),
    ("Процессы и службы", "sudo reboot", "Перезагрузить компьютер.", True),
    ("Процессы и службы", "sudo shutdown now", "Выключить компьютер немедленно.", True),
    # ── Прочее
    ("Полезное", "history", "История ваших команд. Ctrl+R — поиск по ней.", False),
    ("Полезное", "man <команда>", "Справка по команде (q — выход).", False),
    ("Полезное", "<команда> --help", "Краткая подсказка по команде.", False),
    ("Полезное", "sudo !!", "Повторить предыдущую команду с sudo.", False),
    ("Полезное", "which <команда>", "Где лежит программа.", False),
    ("Полезное", "echo $PATH", "Список папок, где система ищет программы.", False),
    ("Полезное", "clear", "Очистить экран терминала (Ctrl+L).", False),
    ("Полезное", "sudo ufw status", "Состояние брандмауэра.", False),
    ("Полезное", "sudo ufw enable", "Включить брандмауэр.", False),
    ("Полезное", "gsettings list-recursively org.gnome.desktop.wm.keybindings",
     "Показать все клавиатурные сочетания управления окнами GNOME.", False),
]

# ════════════════════════════════════════════════════════════════════
#  ГОРЯЧИЕ КЛАВИШИ: что анализируем
# ════════════════════════════════════════════════════════════════════
DOCK_KEYS = {"shortcut", "app-hotkey-1"}
SCHEMAS = [
    ("org.gnome.desktop.wm.keybindings", "🪟 Окна и рабочие столы", None),
    ("org.gnome.shell.keybindings", "🐚 GNOME Shell", None),
    ("org.gnome.mutter.keybindings", "🧱 Mutter: мозаика и мониторы", None),
    ("org.gnome.mutter.wayland.keybindings", "🌊 Wayland", None),
    ("org.gnome.settings-daemon.plugins.media-keys", "🎛 Система и мультимедиа", None),
    ("org.gnome.shell.extensions.dash-to-dock", "⚓ Док", DOCK_KEYS),
]
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"

KEY_RU = {
    # окна
    "close": "Закрыть окно", "minimize": "Свернуть окно", "maximize": "Развернуть окно",
    "unmaximize": "Восстановить размер окна", "toggle-maximized": "Развернуть / восстановить окно",
    "toggle-fullscreen": "Полноэкранный режим", "show-desktop": "Показать рабочий стол",
    "switch-windows": "Переключение между окнами", "switch-windows-backward": "Переключение между окнами (назад)",
    "switch-applications": "Переключение между приложениями",
    "switch-applications-backward": "Переключение между приложениями (назад)",
    "switch-group": "Переключение между окнами одного приложения",
    "switch-group-backward": "Окна одного приложения (назад)",
    "cycle-windows": "Циклический перебор окон", "cycle-windows-backward": "Перебор окон (назад)",
    "cycle-group": "Перебор окон группы", "cycle-group-backward": "Перебор окон группы (назад)",
    "switch-to-workspace-left": "Рабочий стол слева", "switch-to-workspace-right": "Рабочий стол справа",
    "switch-to-workspace-up": "Рабочий стол выше", "switch-to-workspace-down": "Рабочий стол ниже",
    "switch-to-workspace-last": "Последний рабочий стол",
    "move-to-workspace-left": "Переместить окно на рабочий стол слева",
    "move-to-workspace-right": "Переместить окно на рабочий стол справа",
    "move-to-workspace-up": "Переместить окно на рабочий стол выше",
    "move-to-workspace-down": "Переместить окно на рабочий стол ниже",
    "move-to-workspace-last": "Переместить окно на последний рабочий стол",
    "move-to-monitor-left": "Окно на монитор слева", "move-to-monitor-right": "Окно на монитор справа",
    "move-to-monitor-up": "Окно на монитор выше", "move-to-monitor-down": "Окно на монитор ниже",
    "toggle-on-all-workspaces": "Окно на всех рабочих столах",
    "activate-window-menu": "Меню окна", "begin-move": "Переместить окно с клавиатуры",
    "begin-resize": "Изменить размер окна с клавиатуры", "lower": "Опустить окно под остальные",
    "raise": "Поднять окно над остальными", "raise-or-lower": "Поднять / опустить окно",
    "panel-run-dialog": "Окно «Выполнить команду»", "panel-main-menu": "Главное меню",
    "switch-input-source": "Следующая раскладка клавиатуры",
    "switch-input-source-backward": "Предыдущая раскладка клавиатуры",
    # shell
    "toggle-overview": "Обзор окон (Activities)", "toggle-application-view": "Все приложения",
    "toggle-message-tray": "Календарь и уведомления", "focus-active-notification": "Фокус на уведомлении",
    "screenshot": "Снимок всего экрана", "screenshot-window": "Снимок окна",
    "show-screenshot-ui": "Скриншот / запись экрана (интерфейс)",
    "show-screen-recording-ui": "Запись экрана", "toggle-quick-settings": "Быстрые настройки",
    "open-application-menu": "Меню приложения", "shift-overview-up": "Обзор: сдвиг вверх",
    "shift-overview-down": "Обзор: сдвиг вниз", "toggle-tiled-left": "Прижать окно к левой половине",
    "toggle-tiled-right": "Прижать окно к правой половине", "switch-monitor": "Режим мониторов (дублировать/расширить)",
    "rotate-monitor": "Повернуть экран", "cancel-input-capture": "Отменить захват ввода",
    # media-keys
    "terminal": "Открыть терминал", "home": "Домашняя папка", "www": "Открыть браузер",
    "email": "Почта", "calculator": "Калькулятор", "search": "Поиск", "help": "Справка",
    "screensaver": "Заблокировать экран", "logout": "Меню выключения / выхода",
    "volume-up": "Громкость +", "volume-down": "Громкость −", "volume-mute": "Выключить звук",
    "mic-mute": "Выключить микрофон", "play": "Воспроизведение", "pause": "Пауза",
    "stop": "Стоп", "next": "Следующий трек", "previous": "Предыдущий трек", "eject": "Извлечь диск",
    "screenreader": "Экранный диктор", "magnifier": "Лупа (экранная)",
    "magnifier-zoom-in": "Лупа: приблизить", "magnifier-zoom-out": "Лупа: отдалить",
    "on-screen-keyboard": "Экранная клавиатура", "increase-text-size": "Увеличить текст",
    "decrease-text-size": "Уменьшить текст", "toggle-contrast": "Высокий контраст",
    "control-center": "Параметры системы", "touchpad-toggle": "Вкл/выкл тачпад",
    "touchpad-on": "Включить тачпад", "touchpad-off": "Выключить тачпад",
    "screen-brightness-up": "Яркость экрана +", "screen-brightness-down": "Яркость экрана −",
    "keyboard-brightness-up": "Подсветка клавиатуры +", "keyboard-brightness-down": "Подсветка клавиатуры −",
    "keyboard-brightness-toggle": "Подсветка клавиатуры вкл/выкл", "power": "Кнопка питания",
    "suspend": "Ждущий режим", "hibernate": "Гибернация", "rfkill": "Режим «в самолёте»",
    "rotate-video-lock": "Блокировка поворота экрана", "playback-rewind": "Перемотка назад",
    "playback-forward": "Перемотка вперёд", "playback-repeat": "Повтор", "playback-random": "Случайный порядок",
    "media": "Медиаплеер", "battery": "Показать заряд батареи", "shortcut": "Показать док (фокус на док)",
    "app-hotkey-1": "Запустить приложение из дока по номеру (Super+1 … Super+9)",
}
KEY_PATTERNS = [
    (r"switch-to-workspace-(\d+)", "Перейти на рабочий стол {}"),
    (r"move-to-workspace-(\d+)", "Переместить окно на рабочий стол {}"),
    (r"switch-to-application-(\d+)", "Открыть приложение №{} из дока"),
]

MODS = {"Primary": "Ctrl", "Control": "Ctrl", "Ctrl": "Ctrl", "Shift": "Shift", "Alt": "Alt",
        "Mod1": "Alt", "Super": "Super", "Mod4": "Super", "Meta": "Meta", "Hyper": "Hyper",
        "Release": "(отпустить)"}
KEYS = {"Return": "Enter", "Page_Up": "PgUp", "Page_Down": "PgDn", "Prior": "PgUp", "Next": "PgDn",
        "Print": "PrtSc", "Escape": "Esc", "space": "Space", "minus": "−", "plus": "+",
        "Above_Tab": "` (над Tab)", "grave": "`", "Left": "←", "Right": "→", "Up": "↑", "Down": "↓",
        "BackSpace": "⌫", "Delete": "Del", "comma": ",", "period": ".", "slash": "/",
        "bracketleft": "[", "bracketright": "]", "Super_L": "Super", "Super_R": "Super",
        "XF86AudioRaiseVolume": "Vol+", "XF86AudioLowerVolume": "Vol−", "XF86AudioMute": "Mute",
        "XF86AudioPlay": "Play", "XF86AudioPause": "Pause", "XF86AudioStop": "Stop",
        "XF86AudioNext": "Next", "XF86AudioPrev": "Prev", "XF86MonBrightnessUp": "Bright+",
        "XF86MonBrightnessDown": "Bright−", "XF86AudioMicMute": "MicMute", "XF86Calculator": "Calc",
        "XF86WWW": "WWW", "XF86Mail": "Mail", "XF86HomePage": "Home", "XF86Search": "Search"}

TERMINAL_SHORTCUTS = [
    ("Скопировать в терминале", "", ["<Primary><Shift>c"]),
    ("Вставить в терминале", "", ["<Primary><Shift>v"]),
    ("Новая вкладка терминала", "", ["<Primary><Shift>t"]),
    ("Новое окно терминала", "", ["<Primary><Shift>n"]),
    ("Прервать запущенную команду", "SIGINT", ["<Primary>c"]),
    ("Конец ввода / выйти из терминала", "EOF", ["<Primary>d"]),
    ("Очистить экран", "clear", ["<Primary>l"]),
    ("Поиск по истории команд", "reverse-search", ["<Primary>r"]),
    ("В начало строки", "", ["<Primary>a"]),
    ("В конец строки", "", ["<Primary>e"]),
    ("Удалить всё слева от курсора", "", ["<Primary>u"]),
    ("Удалить всё справа от курсора", "", ["<Primary>k"]),
    ("Удалить слово слева", "", ["<Primary>w"]),
    ("Автодополнение имён и команд", "", ["Tab"]),
    ("Приостановить программу (вернуть: fg)", "SIGTSTP", ["<Primary>z"]),
    ("Последний аргумент предыдущей команды", "", ["<Alt>period"]),
]
GENERAL_SHORTCUTS = [
    ("Копировать", "", ["<Primary>c"]), ("Вставить", "", ["<Primary>v"]),
    ("Вырезать", "", ["<Primary>x"]), ("Отменить действие", "", ["<Primary>z"]),
    ("Выделить всё", "", ["<Primary>a"]), ("Поиск на странице / в файле", "", ["<Primary>f"]),
    ("Сохранить", "", ["<Primary>s"]), ("Новая вкладка в браузере", "", ["<Primary>t"]),
    ("Закрыть вкладку", "", ["<Primary>w"]),
]


def looks_accel(a: str) -> bool:
    if not a or a == "disabled" or a.startswith("/"):
        return False
    if "<" in a:
        return True
    return bool(re.fullmatch(r"(XF86\w+|F\d{1,2}|Print|Pause|Home|Escape|Cancel|Super_[LR]|Num_Lock|Scroll_Lock)", a))


def parse_accels(val: str):
    v = val.strip()
    if v.startswith("@as"):
        v = v[3:].strip()
    try:
        x = ast.literal_eval(v)
    except Exception:
        return []
    if isinstance(x, str):
        x = [x]
    if not isinstance(x, (list, tuple)):
        return []
    return [a for a in x if isinstance(a, str) and looks_accel(a)]


def pretty_accel(accel: str):
    mods = re.findall(r"<(\w+)>", accel)
    key = re.sub(r"<\w+>", "", accel)
    parts = [MODS.get(m, m) for m in mods]
    if key:
        if key in KEYS:
            k = KEYS[key]
        elif key.startswith("XF86"):
            k = key[4:]
        elif len(key) == 1:
            k = key.upper()
        else:
            k = key
        parts.append(k)
    return parts


def describe_key(schema, key, gcc, summaries):
    if key in KEY_RU:
        return KEY_RU[key]
    for pat, tpl in KEY_PATTERNS:
        m = re.fullmatch(pat, key)
        if m:
            return tpl.format(m.group(1))
    return gcc.get((schema, key)) or summaries.get((schema, key)) or key


def run_out(cmd, timeout=6):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def load_summaries(ids):
    res = {}
    for p in glob.glob("/usr/share/glib-2.0/schemas/*.gschema.xml"):
        try:
            txt = open(p, encoding="utf-8").read()
            if not any(f'"{i}"' in txt for i in ids):
                continue
            root = ET.fromstring(txt)
        except Exception:
            continue
        for sc in root.iter("schema"):
            sid = sc.get("id")
            if sid not in ids:
                continue
            for k in sc.findall("key"):
                s = k.find("summary")
                if s is not None and s.text:
                    res[(sid, k.get("name"))] = s.text.strip()
    return res


def load_gcc_descriptions():
    res = {}
    for p in glob.glob("/usr/share/gnome-control-center/keybindings/*.xml"):
        try:
            root = ET.parse(p).getroot()
        except Exception:
            continue
        for ents in root.iter("KeyListEntries"):
            schema = ents.get("schema")
            for e in ents.findall("KeyListEntry"):
                if schema and e.get("name") and e.get("description"):
                    res[(schema, e.get("name"))] = e.get("description")
    return res


def analyze_shortcuts():
    """Читает реальные комбинации системы. Возвращает (секции, статистика)."""
    stats = {"schemas": 0, "count": 0, "gsettings": True}
    sections = []
    try:
        have = set(run_out(["gsettings", "list-schemas"]).split())
    except Exception:
        have = set()
        stats["gsettings"] = False
    summaries = load_summaries({s[0] for s in SCHEMAS})
    gcc = load_gcc_descriptions()

    for schema, title, allow in SCHEMAS:
        if schema not in have:
            continue
        try:
            out = run_out(["gsettings", "list-recursively", schema])
        except Exception:
            continue
        items = []
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            _, key, val = parts
            if allow and key not in allow:
                continue
            accels = parse_accels(val)
            if accels:
                items.append((describe_key(schema, key, gcc, summaries), schema.split(".")[-1] + " · " + key, accels))
        if items:
            items.sort(key=lambda i: i[0].lower())
            sections.append((title, items))
            stats["schemas"] += 1
            stats["count"] += len(items)

    # пользовательские
    try:
        raw = run_out(["gsettings", "get", CUSTOM_SCHEMA, "custom-keybindings"]).strip()
        if raw.startswith("@as"):
            raw = raw[3:].strip()
        paths = ast.literal_eval(raw) if raw else []
        items = []
        for path in paths:
            sch = f"{CUSTOM_SCHEMA}.custom-keybinding:{path}"
            g = lambda k: ast.literal_eval(run_out(["gsettings", "get", sch, k]).strip() or "''")
            name, cmd, bind = g("name"), g("command"), g("binding")
            if looks_accel(bind):
                items.append((name or "Без названия", f"команда: {cmd}", [bind]))
        if items:
            sections.append(("⭐ Ваши собственные сочетания", items))
            stats["count"] += len(items)
    except Exception:
        pass

    sections.append(("💻 Терминал и командная строка", TERMINAL_SHORTCUTS))
    sections.append(("📝 Общие для программ", GENERAL_SHORTCUTS))
    return sections, stats


# ════════════════════════════════════════════════════════════════════
#  ПЕСОЧНИЦА: файлы, движок, запуск
# ════════════════════════════════════════════════════════════════════
ZSHRC = r'''# EasyHelper sandbox zsh
export LANG=C.UTF-8
export TERM=${TERM:-xterm-256color}
HISTFILE=~/.zsh_history
HISTSIZE=50000
SAVEHIST=50000
if [ -r /opt/easyhelper/seed_history ] && [ ! -s "$HISTFILE" ]; then
  cp /opt/easyhelper/seed_history "$HISTFILE" 2>/dev/null
fi
setopt APPEND_HISTORY SHARE_HISTORY INC_APPEND_HISTORY HIST_IGNORE_DUPS HIST_REDUCE_BLANKS
setopt AUTO_CD INTERACTIVE_COMMENTS PROMPT_SUBST
autoload -Uz compinit && compinit -u -d ~/.zcompdump
zstyle ':completion:*' menu select
zstyle ':completion:*' matcher-list 'm:{a-z}={A-Z}'
zstyle ':completion:*' list-colors ''
autoload -Uz colors && colors
bindkey -e
bindkey '^[[A' history-beginning-search-backward
bindkey '^[OA' history-beginning-search-backward
bindkey '^[[B' history-beginning-search-forward
bindkey '^[OB' history-beginning-search-forward
bindkey '^[[3~' delete-char
bindkey '^[[H' beginning-of-line
bindkey '^[[F' end-of-line
PROMPT='%F{141}sandbox%f%F{245}@%f%F{51}easyhelper%f %F{221}%~%f %F{141}❯%f '
alias ls='ls --color=auto' ll='ls -alF --color=auto' la='ls -A --color=auto' grep='grep --color=auto'
precmd() { print -Pn "\e]0;sandbox:%~\a" }
ZSH_AUTOSUGGEST_STRATEGY=(history completion)
ZSH_AUTOSUGGEST_HIGHLIGHT_STYLE='fg=244'
for f in /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh \
         /usr/share/zsh/plugins/zsh-autosuggestions/zsh-autosuggestions.zsh; do
  [ -r "$f" ] && source "$f" && break
done
# подсветка синтаксиса — строго последней
for f in /usr/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh \
         /usr/share/zsh/plugins/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh; do
  [ -r "$f" ] && source "$f" && break
done
print -P "%F{141}🧪 Песочница EasyHelper%f — это не ваша система, ломайте что хотите."
print -P "%F{245}→ принимает подсказку, ↑/↓ — история, Tab — дополнение%f\n"
'''

DOCKERFILE = '''FROM docker.io/library/ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \\
    zsh zsh-autosuggestions zsh-syntax-highlighting sudo ca-certificates locales \\
    nano vim-tiny curl wget git htop tree less man-db manpages procps psmisc \\
    iputils-ping iproute2 net-tools dnsutils file zip unzip lsb-release \\
    bash-completion pciutils usbutils bsdmainutils unminimize \\
    && (yes | unminimize || true)
RUN useradd -m -s /usr/bin/zsh -G sudo user \\
    && echo 'user ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/user && chmod 0440 /etc/sudoers.d/user
COPY --chown=user:user zshrc /home/user/.zshrc
COPY seed_history /opt/easyhelper/seed_history
USER user
WORKDIR /home/user
CMD ["zsh"]
'''

BOOT_SCRIPT = r'''
engine="$1"; img="$2"; dir="$3"; name="$4"
"$engine" rm -f "$name" >/dev/null 2>&1
if ! "$engine" image inspect "$img" >/dev/null 2>&1; then
  echo "⏳ Первый запуск: собираю образ песочницы (нужен интернет, 2–6 минут)…"
  "$engine" build -t "$img" "$dir" || { echo "❌ Не удалось собрать образ."; read -rsn1 -p "Нажмите любую клавишу…"; exit 1; }
fi
exec "$engine" run --rm -it --name "$name" --hostname easyhelper --memory 1g --pids-limit 512 \
  -e TERM=xterm-256color -e COLORTERM=truecolor "$img"
'''


def prepare_files():
    ctx = APP_DIR / "build"
    ctx.mkdir(parents=True, exist_ok=True)
    seen, hist = set(), []
    for _, cmd, _, danger in COMMANDS:
        if "<" in cmd or danger or cmd in seen:
            continue
        seen.add(cmd)
        hist.append(cmd)
    seed = "\n".join(hist) + "\n"
    (ctx / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    (ctx / "zshrc").write_text(ZSHRC, encoding="utf-8")
    (ctx / "seed_history").write_text(seed, encoding="utf-8")
    (APP_DIR / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\nuser:x:1000:1000:user:/home/user:/usr/bin/zsh\n"
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n", encoding="utf-8")
    (APP_DIR / "group").write_text("root:x:0:\nuser:x:1000:\nnogroup:x:65534:\n", encoding="utf-8")
    h = hashlib.sha1((DOCKERFILE + ZSHRC + seed).encode()).hexdigest()[:8]
    return ctx, f"easyhelper-sandbox:{h}"


def detect_engine():
    for e in ("podman", "docker"):
        if shutil.which(e):
            try:
                if subprocess.run([e, "info"], capture_output=True, timeout=20).returncode == 0:
                    return e
            except Exception:
                pass
    return "bwrap" if shutil.which("bwrap") else None


def bwrap_argv():
    zsh = shutil.which("zsh")
    zsh = os.path.realpath(zsh) if zsh else None
    use_zsh = bool(zsh and zsh.startswith("/usr/"))
    shell = [zsh] if use_zsh else ["/usr/bin/bash", "--noprofile", "--norc", "-i"]
    ctx = APP_DIR / "build"
    a = ["bwrap", "--unshare-all", "--share-net", "--die-with-parent", "--uid", "1000", "--gid", "1000",
         "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
         "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
         "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/home/user",
         "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
         "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
         "--ro-bind-try", "/etc/ssl", "/etc/ssl",
         "--ro-bind-try", "/etc/resolv.conf", "/etc/resolv.conf",
         "--ro-bind-try", "/run/systemd/resolve", "/run/systemd/resolve",
         "--ro-bind-try", "/etc/hosts", "/etc/hosts",
         "--ro-bind", str(APP_DIR / "passwd"), "/etc/passwd", "--ro-bind", str(APP_DIR / "group"), "/etc/group",
         "--ro-bind", str(ctx / "zshrc"), "/home/user/.zshrc",
         "--ro-bind", str(ctx / "seed_history"), "/opt/easyhelper/seed_history",
         "--setenv", "HOME", "/home/user", "--setenv", "USER", "user", "--setenv", "LANG", "C.UTF-8",
         "--setenv", "TERM", "xterm-256color", "--setenv", "COLORTERM", "truecolor",
         "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
         "--setenv", "PS1", r"\[\e[35m\]sandbox\[\e[0m\]@easyhelper \w $ ",
         "--setenv", "PROMPT_COMMAND", r'printf "\033]0;sandbox:%s\007" "$PWD"',
         "--chdir", "/home/user"]
    return a + shell, use_zsh


# ════════════════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНОЕ ДЛЯ VTE
# ════════════════════════════════════════════════════════════════════
def rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def style_terminal(t):
    t.set_font(Pango.FontDescription("Monospace 11"))
    t.set_color_foreground(rgba(0.90, 0.88, 0.94))
    t.set_color_background(rgba(0, 0, 0, 0.62))          # чёрный полупрозрачный
    t.set_color_cursor(rgba(0.66, 0.33, 0.97))
    t.set_scrollback_lines(20000)
    t.set_scroll_on_keystroke(True)
    t.set_audible_bell(False)
    t.set_allow_hyperlink(True)
    t.set_hexpand(True)
    t.set_vexpand(True)
    kc = Gtk.EventControllerKey()
    kc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def pressed(_c, keyval, _code, state):
        m = state & Gtk.accelerator_get_default_mod_mask()
        if m == (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            if keyval in (Gdk.KEY_C, Gdk.KEY_c):
                t.copy_clipboard_format(Vte.Format.TEXT)
                return True
            if keyval in (Gdk.KEY_V, Gdk.KEY_v):
                t.paste_clipboard()
                return True
        return False

    kc.connect("key-pressed", pressed)
    t.add_controller(kc)


def spawn(term, argv, on_pid=None, cwd=None):
    def cb(_t, pid, err, *_a):
        if err:
            print("spawn error:", err)
        elif on_pid:
            on_pid(pid)

    args = (Vte.PtyFlags.DEFAULT, cwd, argv, None, GLib.SpawnFlags.DEFAULT, None, None, -1, None, cb)
    try:
        term.spawn_async(*args)
    except TypeError:
        term.spawn_async(*args, None)


def term_feed_text(term, text):
    data = text.replace("\n", "\r\n").encode()
    try:
        term.feed(data)
    except TypeError:
        term.feed(data.decode(), -1)


def term_feed_child(term, data: bytes):
    try:
        term.feed_child(data)
    except TypeError:
        term.feed_child(data.decode(), -1)


# ════════════════════════════════════════════════════════════════════
#  ЛОГОТИП
# ════════════════════════════════════════════════════════════════════
def load_logo(height=64):
    """Картинка-логотип. Если файла нет или он битый — возвращает None."""
    try:
        if not os.path.isfile(LOGO_PATH):
            return None
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(LOGO_PATH, -1, height, True)
        pic = Gtk.Picture.new_for_paintable(Gdk.Texture.new_for_pixbuf(pb))
        pic.set_can_shrink(False)
        pic.set_halign(Gtk.Align.START)
        pic.set_valign(Gtk.Align.CENTER)
        return pic
    except Exception as e:  # noqa
        print("logo error:", e)
        return None


# ════════════════════════════════════════════════════════════════════
#  UI-КОМПОНЕНТЫ
# ════════════════════════════════════════════════════════════════════
def make_card():
    lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    lb.add_css_class("card-list")
    return lb


def make_section(title):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(Gtk.Label(label=title, xalign=0, css_classes=["section-title"]))
    lb = make_card()
    box.append(lb)
    return box, lb


def keycaps_box(accel):
    box = Gtk.Box(spacing=4, halign=Gtk.Align.END)
    parts = pretty_accel(accel)
    for i, p in enumerate(parts):
        if i:
            box.append(Gtk.Label(label="+", css_classes=["dim"]))
        box.append(Gtk.Label(label=p, css_classes=["keycap"]))
    return box


def page_scaffold(search_placeholder):
    """Вертикальный контейнер: поиск сверху + прокручиваемый список по центру."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    search = Gtk.SearchEntry(placeholder_text=search_placeholder)
    status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim"])
    top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=12, margin_end=12, margin_top=6)
    top.append(search)
    top.append(status)
    outer.append(Adw.Clamp(maximum_size=760, child=top))
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_start=12, margin_end=12,
                      margin_top=6, margin_bottom=20)
    scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroll.set_child(Adw.Clamp(maximum_size=760, child=content))
    outer.append(scroll)
    return outer, search, status, content


class ShortcutsPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("opaque-bg")
        outer, self.search, self.status, self.content = page_scaffold("Поиск: действие или клавиша…")
        outer.set_vexpand(True)
        self.append(outer)
        self.sections = []
        self.search.connect("search-changed", self.on_search)
        self.status.set_text("🔍 Анализирую настройки GNOME…")
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        try:
            res = analyze_shortcuts()
        except Exception as e:  # noqa
            res = ([("💻 Терминал и командная строка", TERMINAL_SHORTCUTS)], {"schemas": 0, "count": 0, "gsettings": False})
            print("analyze error:", e)
        GLib.idle_add(self._render, *res)

    def _render(self, sections, stats):
        if stats["gsettings"] and stats["count"]:
            self.status.set_text(
                f"✔ Найдено комбинаций: {stats['count']} · схем GNOME: {stats['schemas']}. "
                "Источники: dconf/gsettings, /usr/share/glib-2.0/schemas, gnome-control-center.")
            self.status.add_css_class("ok")
        else:
            self.status.set_text("⚠ GNOME/gsettings не найден — показаны только универсальные комбинации.")
            self.status.add_css_class("warn")
        for title, items in sections:
            box, lb = make_section(title)
            rows = []
            for desc, sub, accels in items:
                row = Gtk.ListBoxRow(activatable=False)
                h = Gtk.Box(spacing=12)
                left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
                left.append(Gtk.Label(label=desc, xalign=0, wrap=True))
                if sub:
                    left.append(Gtk.Label(label=sub, xalign=0, wrap=True, css_classes=["dim"]))
                right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER,
                                halign=Gtk.Align.END)
                for a in accels:
                    right.append(keycaps_box(a))
                h.append(left)
                h.append(right)
                row.set_child(h)
                lb.append(row)
                text = (desc + " " + sub + " " + " ".join(" ".join(pretty_accel(a)) for a in accels)).lower()
                rows.append((row, text))
            self.content.append(box)
            self.sections.append((box, rows))
        return False

    def on_search(self, entry):
        q = entry.get_text().strip().lower()
        for box, rows in self.sections:
            any_vis = False
            for row, text in rows:
                vis = q in text
                row.set_visible(vis)
                any_vis |= vis
            box.set_visible(any_vis)


class CommandsPage(Gtk.Box):
    def __init__(self, on_run):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("opaque-bg")
        outer, self.search, status, self.content = page_scaffold("Поиск команды или описания…")
        status.set_text("Нажмите «Выполнить» — вы сможете отредактировать команду и выбрать: система или песочница.")
        outer.set_vexpand(True)
        self.append(outer)
        self.sections = []
        cats = {}
        for cat, cmd, desc, danger in COMMANDS:
            cats.setdefault(cat, []).append((cmd, desc, danger))
        for cat, items in cats.items():
            box, lb = make_section(cat)
            rows = []
            for cmd, desc, danger in items:
                row = Gtk.ListBoxRow(activatable=False)
                h = Gtk.Box(spacing=12)
                left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
                left.append(Gtk.Label(label=cmd, xalign=0, wrap=True, selectable=True, css_classes=["cmd"]))
                left.append(Gtk.Label(label=desc, xalign=0, wrap=True, css_classes=["dim"]))
                btn = Gtk.Button(label="▶ Выполнить", valign=Gtk.Align.CENTER, css_classes=["accent-btn"])
                btn.connect("clicked", lambda _b, c=cmd, d=desc, dg=danger: on_run(c, d, dg))
                h.append(left)
                h.append(btn)
                row.set_child(h)
                lb.append(row)
                rows.append((row, (cmd + " " + desc).lower()))
            self.content.append(box)
            self.sections.append((box, rows))
        self.search.connect("search-changed", self.on_search)

    def on_search(self, entry):
        q = entry.get_text().strip().lower()
        for box, rows in self.sections:
            any_vis = False
            for row, text in rows:
                vis = q in text
                row.set_visible(vis)
                any_vis |= vis
            box.set_visible(any_vis)


class JournalPage(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=0, margin_end=0, margin_top=0)
        self.add_css_class("opaque-bg")
        self.empty = Gtk.Label(label="Пока пусто. Запущенные команды появятся здесь.", css_classes=["dim"],
                               vexpand=True)
        self.lb = make_card()
        self.lb.set_visible(False)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_bottom=16,
                        margin_start=12, margin_end=12)
        inner.append(self.lb)
        scroll.set_child(Adw.Clamp(maximum_size=760, child=inner))
        clear = Gtk.Button(label="Очистить журнал", css_classes=["ghost-btn"], halign=Gtk.Align.END,
                           margin_start=12, margin_end=12, margin_top=6)
        clear.connect("clicked", self.clear)
        self.append(Adw.Clamp(maximum_size=760, child=clear))
        self.append(self.empty)
        self.append(scroll)

    def add(self, where, cmd):
        self.empty.set_visible(False)
        self.lb.set_visible(True)
        row = Gtk.ListBoxRow(activatable=False)
        h = Gtk.Box(spacing=10)
        h.append(Gtk.Label(label=time.strftime("%H:%M:%S"), css_classes=["dim"]))
        h.append(Gtk.Label(label=where, css_classes=["keycap"]))
        h.append(Gtk.Label(label=cmd, xalign=0, hexpand=True, wrap=True, selectable=True, css_classes=["cmd"]))
        row.set_child(h)
        self.lb.prepend(row)

    def clear(self, *_):
        while (r := self.lb.get_first_child()):
            self.lb.remove(r)
        self.lb.set_visible(False)
        self.empty.set_visible(True)


class SandboxPage(Gtk.Box):
    """Страница НЕ закрашена: сквозь терминал виден рабочий стол.
    Полоса статуса сверху и подсказка снизу закрашены непрозрачно."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.engine = None
        self.started = False
        self.ready = False
        self.alive = False
        self.suppress_exit = False
        self.pending = []
        self.pid = None

        bar = Gtk.Box(spacing=8, css_classes=["opaque-bg", "pad-bar"])
        self.status = Gtk.Label(xalign=0, hexpand=True, wrap=True, css_classes=["dim"],
                                label="Песочница запустится при открытии вкладки.")
        restart = Gtk.Button(label="↻ Новая сессия", css_classes=["ghost-btn"])
        restart.connect("clicked", lambda *_: self.restart())
        bar.append(self.status)
        bar.append(restart)
        self.append(bar)

        self.term = Vte.Terminal()
        style_terminal(self.term)
        self.term.connect("window-title-changed", self.on_title)
        self.term.connect("child-exited", self.on_exit)
        frame = Gtk.ScrolledWindow(vexpand=True, css_classes=["terminal-frame"])
        frame.set_child(self.term)
        self.append(frame)

        self.append(Gtk.Label(
            xalign=0, wrap=True, css_classes=["dim", "opaque-bg", "pad-bar"],
            label="Копировать/вставить: Ctrl+Shift+C / Ctrl+Shift+V. Это изолированная среда — "
                  "системные службы (systemctl, ufw) и реальные диски здесь не работают."))

    # --- запуск
    def start(self):
        if self.started:
            return
        self.started = True
        self.ready = False
        self.status.set_text("⏳ Подготавливаю песочницу…")
        threading.Thread(target=self._detect, daemon=True).start()

    def _detect(self):
        try:
            ctx, image = prepare_files()
            eng = detect_engine()
        except Exception as e:  # noqa
            ctx, image, eng = None, None, None
            print(e)
        GLib.idle_add(self._spawn, eng, ctx, image)

    def _spawn(self, eng, ctx, image):
        self.engine = eng
        if eng is None:
            self.status.set_text("❌ Нет podman / docker / bubblewrap.")
            term_feed_text(self.term,
                           "Для песочницы установите один из движков:\n\n"
                           "  sudo apt install podman            (лучший вариант, полноценный Ubuntu)\n"
                           "  sudo apt install bubblewrap zsh zsh-autosuggestions zsh-syntax-highlighting\n\n"
                           "и нажмите «Новая сессия».\n")
            return False
        if eng == "bwrap":
            argv, use_zsh = bwrap_argv()
            self.status.set_text("🧪 Режим bubblewrap (лёгкая изоляция, без apt install)"
                                 + ("" if use_zsh else " · zsh не найден, используется bash"))
        else:
            argv = ["bash", "-c", BOOT_SCRIPT, "easyhelper", eng, image, str(ctx), CONTAINER]
            self.status.set_text(f"🧪 Контейнер Ubuntu 24.04 через {eng} · изменения исчезают после сессии")
        self.alive = True
        spawn(self.term, argv, on_pid=lambda p: setattr(self, "pid", p))
        return False

    def restart(self):
        if self.alive and self.pid:
            self.suppress_exit = True
            try:
                os.kill(self.pid, signal.SIGHUP)
            except Exception:
                self.suppress_exit = False
        self.term.reset(True, True)
        self.started = False
        self.pending.clear()
        self.start()

    def shutdown(self):
        if self.engine in ("podman", "docker"):
            subprocess.Popen([self.engine, "rm", "-f", CONTAINER], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

    # --- события
    def on_title(self, term):
        title = term.get_window_title() or ""
        if title.startswith("sandbox") and not self.ready:
            self.ready = True
            self.status.set_text(self.status.get_text().replace("⏳", "✔"))
            data, self.pending = self.pending, []
            for d in data:
                GLib.timeout_add(300, lambda d=d: (term_feed_child(self.term, d), False)[1])

    def on_exit(self, *_):
        self.alive = False
        if self.suppress_exit:
            self.suppress_exit = False
            return
        self.ready = False
        self.started = False
        term_feed_text(self.term, "\n[Сессия завершена — нажмите «↻ Новая сессия»]\n")

    def send(self, cmd):
        self.start()
        data = (cmd + "\n").encode()
        if self.ready:
            term_feed_child(self.term, data)
        else:
            self.pending.append(data)
        self.term.grab_focus()


# ════════════════════════════════════════════════════════════════════
#  ДИАЛОГ «ВЫПОЛНИТЬ» и СИСТЕМНЫЙ ЗАПУСК
# ════════════════════════════════════════════════════════════════════
class RunDialog(Gtk.Window):
    def __init__(self, parent, cmd, desc, danger, on_choice):
        super().__init__(transient_for=parent, modal=True, title="Выполнить команду",
                         default_width=560, resizable=False)
        self.add_css_class("opaque-bg")        # диалог — непрозрачный
        self.on_choice = on_choice
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=20, margin_bottom=20,
                      margin_start=22, margin_end=22)
        box.append(Gtk.Label(label="Где выполнить команду?", xalign=0, css_classes=["section-title"]))
        box.append(Gtk.Label(label=desc, xalign=0, wrap=True, css_classes=["dim"]))
        self.entry = Gtk.Entry(text=cmd, css_classes=["cmd-entry"])
        self.entry.connect("activate", lambda *_: self.choose("sandbox"))
        box.append(self.entry)
        self.msg = Gtk.Label(xalign=0, wrap=True)
        if "<" in cmd:
            self.msg.set_text("✏ Замените части в <угловых скобках> на своё значение.")
            self.msg.add_css_class("dim")
        box.append(self.msg)
        if danger:
            box.append(Gtk.Label(label="⚠ Опасная команда! В системе она может безвозвратно удалить данные "
                                       "или выключить компьютер. Для проб лучше песочница.",
                                 xalign=0, wrap=True, css_classes=["warn"]))
        btns = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6)
        cancel = Gtk.Button(label="Отмена", css_classes=["ghost-btn"])
        cancel.connect("clicked", lambda *_: self.close())
        sb = Gtk.Button(label="🧪 В песочнице", css_classes=["accent-btn"])
        sb.connect("clicked", lambda *_: self.choose("sandbox"))
        sy = Gtk.Button(label="🖥 В системе", css_classes=["danger-btn" if danger else "ghost-btn"])
        sy.connect("clicked", lambda *_: self.choose("system"))
        for b in (cancel, sy, sb):
            btns.append(b)
        box.append(btns)
        self.set_child(box)
        ctl = Gtk.EventControllerKey()
        ctl.connect("key-pressed", lambda _c, kv, *_: (self.close(), True)[1] if kv == Gdk.KEY_Escape else False)
        self.add_controller(ctl)

    def choose(self, target):
        cmd = self.entry.get_text().strip()
        if not cmd:
            return
        if re.search(r"<[^<>]+>", cmd):
            self.msg.set_text("⚠ Сначала замените <поля в скобках> на реальные значения.")
            self.msg.remove_css_class("dim")
            self.msg.add_css_class("warn")
            return
        self.close()
        self.on_choice(target, cmd)


class SystemRunWindow(Gtk.Window):
    """Окно с обычным терминалом — тоже полупрозрачное."""

    def __init__(self, parent, cmd):
        super().__init__(transient_for=parent, title=f"Система — {cmd}", default_width=840, default_height=460)
        t = Vte.Terminal()
        style_terminal(t)
        t.connect("child-exited", lambda *_: self.close())
        sw = Gtk.ScrolledWindow()
        sw.set_child(t)
        self.set_child(sw)
        script = ('eval "$1"; rc=$?; echo; echo "── Завершено (код $rc) ──"; '
                  'read -rsn1 -p "Нажмите любую клавишу, чтобы закрыть…"')
        spawn(t, ["bash", "-c", script, "easyhelper", cmd], cwd=GLib.get_home_dir())


# ════════════════════════════════════════════════════════════════════
#  ГЛАВНОЕ ОКНО
# ════════════════════════════════════════════════════════════════════
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="EasyHelper", default_width=900, default_height=800)
        self.journal = JournalPage()
        self.sandbox = SandboxPage()
        self.shortcuts = ShortcutsPage()
        self.commands = CommandsPage(self.request_run)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.stack.add_titled(self.shortcuts, "keys", "⌨ Горячие клавиши")
        self.stack.add_titled(self.commands, "cmds", "📦 Команды")
        self.stack.add_titled(self.sandbox, "sandbox", "🧪 Песочница")
        self.stack.add_titled(self.journal, "journal", "📜 Журнал")
        self.stack.connect("notify::visible-child-name", self.on_page)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.StackSwitcher(stack=self.stack))

        # ── заголовок приложения: логотип + название + описание
        title_row = Gtk.Box(spacing=14)
        logo = load_logo(64)
        if logo:
            title_row.append(logo)
        title_row.append(Gtk.Label(label="EasyHelper", xalign=0, valign=Gtk.Align.CENTER,
                                   css_classes=["app-title"]))

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=14, margin_bottom=10,
                       margin_start=20, margin_end=20)
        head.append(title_row)
        head.append(Gtk.Label(
            xalign=0, wrap=True, css_classes=["app-sub"],
            label="Помощник для новичков в PulseOS. Показывает полезные горячие клавиши вашей системы, "
                  "объясняет популярные команды простыми словами и даёт безопасную песочницу-терминал, "
                  "где можно пробовать что угодно."))

        head_wrap = Gtk.Box(css_classes=["opaque-bg"])
        head_wrap.append(Adw.Clamp(maximum_size=760, child=head, hexpand=True))

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(header)
        root.append(head_wrap)
        root.append(self.stack)
        self.set_content(root)
        self.connect("close-request", self.on_close)

    def on_page(self, *_):
        if self.stack.get_visible_child_name() == "sandbox":
            self.sandbox.start()

    def on_close(self, *_):
        self.sandbox.shutdown()
        return False

    def request_run(self, cmd, desc, danger):
        RunDialog(self, cmd, desc, danger, self.do_run).present()

    def do_run(self, target, cmd):
        if target == "sandbox":
            self.journal.add("песочница", cmd)
            self.stack.set_visible_child_name("sandbox")
            self.sandbox.send(cmd)
        else:
            self.journal.add("система", cmd)
            SystemRunWindow(self, cmd).present()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        prov = Gtk.CssProvider()
        if hasattr(prov, "load_from_string"):
            prov.load_from_string(CSS)
        else:
            prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = self.props.active_window or MainWindow(self)
        win.present()


if __name__ == "__main__":
    App().run(None)
