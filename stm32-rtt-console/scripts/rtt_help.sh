#!/bin/sh
# Полная справка по RTT в OpenOCD: точные имена подкоманд и параметры.
. "$HOME/.local/stm32-gdbtest/env.sh" 2>/dev/null
openocd -c "help rtt" -c "help \"rtt setup\"" -c "help \"rtt start\"" -c "help \"rtt channels\"" -c "shutdown" 2>&1 | grep -v -E 'Licensed|For bug|openocd.org|^$'
