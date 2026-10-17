#!/bin/sh
# Полная справка по RTT в OpenOCD: точные имена подкоманд и параметры.
# Окружение стенда stm32-gdbtest подключается, если установлено.
ENV_SH="$HOME/.local/stm32-gdbtest/env.sh"
[ -f "$ENV_SH" ] && . "$ENV_SH"
TARGET="${TARGET:-target/stm32f4x.cfg}"   # rtt setup/start есть только вместе с целью
openocd -c "adapter driver dummy" -f "$TARGET" -c "help rtt" -c "help \"rtt setup\"" -c "help \"rtt start\"" -c "help \"rtt channels\"" -c "shutdown" 2>&1 | grep -v -E 'Licensed|For bug|openocd.org|^$'
