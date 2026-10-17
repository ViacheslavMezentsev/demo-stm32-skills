#!/bin/sh
# Проверить, реально ли OpenOCD 0.12 поддерживает RTT: какие команды есть и что они делают.
# RTT в OpenOCD появился в 0.11, поэтому на 0.12 он должен быть; проверяем не по документации, а
# по справке команд и по реакции на них. Команды rtt setup/start/channels регистрируются только
# вместе с целью, поэтому справка запрашивается с target-файлом и фиктивным адаптером (пробник не нужен).
TARGET="${TARGET:-target/stm32f4x.cfg}"
# Окружение стенда stm32-gdbtest подключается, если установлено.
ENV_SH="$HOME/.local/stm32-gdbtest/env.sh"
[ -f "$ENV_SH" ] && . "$ENV_SH"

echo "=== версия ==="
openocd --version 2>&1 | head -2

echo "=== справка: есть ли rtt ==="
openocd -c "adapter driver dummy" -f "$TARGET" -c "help rtt" -c "shutdown" 2>&1 | grep -E "^ +rtt " | head -12

echo "=== справка по подкомандам ==="
for cmd in "rtt setup" "rtt start" "rtt stop" "rtt channels" "rtt server"; do
    echo "--- $cmd"
    openocd -c "adapter driver dummy" -f "$TARGET" -c "help $cmd" -c "shutdown" 2>&1 | grep -E "^ +rtt " | head -6
done
