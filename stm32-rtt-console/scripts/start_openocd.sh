#!/bin/sh
# Поднять OpenOCD на стенде для PoC RTT: GDB-порт и telnet для команд RTT.
#
# Порядок принципиален: RTT настраивается ПОСЛЕ того, как блок появится в памяти (его создаёт инъекция
# из GDB). Поэтому сервер поднимается без RTT, а команды `rtt setup/start` посылаются отдельно через
# telnet — их выполняет `rtt_read.sh` после инъекции.
#
# Параметры: интерфейс stlink, target stm32f4x, скорость 1000 кГц. Серийный номер пробника — первым
# аргументом или в STLINK_SERIAL; без него OpenOCD берёт первый найденный ST-Link.
#   sh start_openocd.sh [серийный номер] [gdb-порт] [telnet-порт] [каталог журнала]
# Окружение стенда stm32-gdbtest подключается, если установлено.
ENV_SH="$HOME/.local/stm32-gdbtest/env.sh"
[ -f "$ENV_SH" ] && . "$ENV_SH"

SERIAL="${1:-${STLINK_SERIAL:-}}"
GDB_PORT="${2:-3333}"
TELNET_PORT="${3:-4444}"
LOG="${4:-/tmp/rtt-poc}"
mkdir -p "$LOG"

# Занят ли пробник другим сервером (без серийного номера — любой OpenOCD этого пользователя).
if pgrep -f "openocd.*$SERIAL" > /dev/null 2>&1; then
    echo "OpenOCD уже работает с этим пробником, останавливаю"
    pkill -f "openocd.*$SERIAL" 2>/dev/null
    sleep 1
fi

# Выбор пробника по серийному номеру (без номера — пустая команда Tcl).
SERIAL_CMD="adapter serial $SERIAL"
[ -n "$SERIAL" ] || SERIAL_CMD="#"

echo "=== запуск OpenOCD ==="
nohup openocd \
    -f interface/stlink.cfg \
    -c "$SERIAL_CMD" \
    -f target/stm32f4x.cfg \
    -c "adapter speed 1000" \
    -c "bindto 127.0.0.1" \
    -c "gdb_port $GDB_PORT" \
    -c "tcl_port disabled" \
    -c "telnet_port $TELNET_PORT" \
    -c "init" \
    -c "reset halt" \
    > "$LOG/openocd.log" 2>&1 &
echo $! > "$LOG/openocd.pid"

for attempt in $(seq 1 40); do
    if grep -q "Listening on port $GDB_PORT" "$LOG/openocd.log" 2>/dev/null; then
        echo "  GDB-порт $GDB_PORT готов (попытка $attempt)"
        break
    fi
    sleep 0.5
done
if grep -q "Listening on port $TELNET_PORT" "$LOG/openocd.log" 2>/dev/null; then
    echo "  telnet-порт $TELNET_PORT готов"
fi

echo "=== состояние ==="
grep -E "Listening|Info : stm32|halted|target" "$LOG/openocd.log" | head -8
