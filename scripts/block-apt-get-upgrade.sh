#!/bin/bash

# Проверяем, установлена ли nala. Если нет - используем стандартный apt.
if ! command -v nala &> /dev/null; then
    /usr/bin/apt "$@"
    exit $?
fi

# Если первая команда - upgrade, включаем режим защиты
if [ "$1" == "upgrade" ]; then
    echo -e "\e[1;31m⚠️  ВНИМАНИЕ! ВЫ НАХОДИТЕСЬ НА ВЕТКЕ devel (ROLLING RELEASE)!\e[0m"
    echo -e "\e[33mКоманда 'apt upgrade' запрещена, так как она не умеет правильно разрешать зависимости при глобальных обновлениях и может сломать систему.\e[0m"
    echo ""
    echo -e "Пожалуйста, выберите один из безопасных вариантов (через nala):"
    echo -e "  \e[1;32m1)\e[0m Выполнить sudo nala update && sudo nala dist-upgrade"
    echo -e "  \e[1;32m2)\e[0m Выполнить sudo nala update && sudo nala full-upgrade"
    echo -e "  \e[1;31m3)\e[0m Отмена"
    echo ""
    read -p "Введите цифру (1, 2 или 3): " choice

    # Определяем, нужен ли sudo (если скрипт запущен не от рута)
    if [ "$EUID" -ne 0 ]; then
        SUDO_CMD="sudo"
    else
        SUDO_CMD=""
    fi

    case $choice in
        1) $SUDO_CMD /usr/bin/nala update && $SUDO_CMD /usr/bin/nala dist-upgrade ;;
        2) $SUDO_CMD /usr/bin/nala update && $SUDO_CMD /usr/bin/nala full-upgrade ;;
        *) echo "Операция отменена." ;;
    esac
else
    # Если это любая другая команда (install, update, remove и т.д.), 
    # молча передаем её в nala.
    /usr/bin/nala "$@"
fi
