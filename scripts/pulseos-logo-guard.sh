#!/bin/bash
# pulseos-logo-guard — восстанавливает логотип PulseOS после обновлений пакетов
set -u

LOGO_SRC="/usr/local/share/pulseos-logo.png"
PIXMAPS="/usr/share/pixmaps"
BACKUP_DIR="$PIXMAPS/ubuntu-logos-original"

[ -f "$LOGO_SRC" ] || exit 0

mkdir -p "$BACKUP_DIR"

NAMES="ubuntu-logo.svg ubuntu-logo-text.png ubuntu-logo-text-dark.png ubuntu-logo-text.svg ubuntu-logo-text-dark.svg"

changed=0
for name in $NAMES; do
    target="$PIXMAPS/$name"
    backup="$BACKUP_DIR/$name"

    if [ ! -f "$backup" ] && [ -f "$target" ] && ! cmp -s "$target" "$LOGO_SRC"; then
        cp -f "$target" "$backup" 2>/dev/null || true
    fi

    if [ ! -f "$target" ] || ! cmp -s "$target" "$LOGO_SRC"; then
        cp -f "$LOGO_SRC" "$target"
        changed=1
    fi
done

if [ "$changed" = "1" ]; then
    pkill -f gnome-control-center 2>/dev/null || true
fi

exit 0
