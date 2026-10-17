#!/bin/sh
# Проверить, реально ли OpenOCD 0.12 поддерживает RTT: какие команды есть и что они делают.
# RTT в OpenOCD появился в 0.11, поэтому на 0.12 он должен быть; проверяем не по документации, а
# по справке команд и по реакции на них.
. "$HOME/.local/stm32-gdbtest/env.sh" 2>/dev/null

echo "=== версия ==="
openocd --version 2>&1 | head -2

echo "=== справка: есть ли rtt ==="
openocd -c "help rtt" -c "shutdown" 2>&1 | head -12

echo "=== справка по подкомандам ==="
for cmd in "rtt setup" "rtt start" "rtt stop" "rtt channels" "rtt server"; do
    echo "--- $cmd"
    openocd -c "help $cmd" -c "shutdown" 2>&1 | head -6
done
