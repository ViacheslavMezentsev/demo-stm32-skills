#!/bin/sh
# Настроить и запустить RTT в уже работающем OpenOCD через telnet, затем прочитать канал.
#
# Вызывается ПОСЛЕ инъекции блока в SRAM: OpenOCD ищет управляющий блок по строке "SEGGER RTT" в
# указанном окне памяти, поэтому блок должен существовать до `rtt setup`.
ENV_SH="$HOME/.local/stm32-gdbtest/env.sh"
[ -f "$ENV_SH" ] && . "$ENV_SH"

TELNET_PORT="${1:-4444}"
LOG="${2:-/tmp/rtt-poc}"
# Окно поиска блока: по умолчанию последние 8 КБ SRAM STM32F411 (как у build_block.py).
RTT_ADDR="${RTT_ADDR:-0x2001E000}"
RTT_SIZE="${RTT_SIZE:-0x1000}"
mkdir -p "$LOG"

# Отправка команд в telnet-порт OpenOCD (без интерактивности).
send() {
    { printf '%s\n' "$1"; sleep 0.3; } | timeout 4 nc -q 1 127.0.0.1 "$TELNET_PORT" 2>/dev/null
}

echo "=== настройка RTT ==="
echo "--- rtt setup $RTT_ADDR $RTT_SIZE \"SEGGER RTT\""
send "rtt setup $RTT_ADDR $RTT_SIZE \"SEGGER RTT\""

echo "--- rtt start"
send 'rtt start'

echo "--- rtt channels"
send 'rtt channels'

echo "--- rtt channellist"
send 'rtt channellist'

echo "=== запуск RTT-сервера на порту 19021 ==="
send 'rtt server start 19021 0'
sleep 0.5

echo "=== чтение потока (5 с) ==="
timeout 5 nc 127.0.0.1 19021 > "$LOG/rtt-read.bin" 2>/dev/null
SIZE=$(wc -c < "$LOG/rtt-read.bin" 2>/dev/null || echo 0)
echo "  получено байт: $SIZE"
if [ "$SIZE" -gt 0 ]; then
    echo "  содержимое:"
    sed 's/^/    /' "$LOG/rtt-read.bin"
    # Показать как hex для однозначности
    echo "  hex:"
    xxd "$LOG/rtt-read.bin" | head -5 | sed 's/^/    /'
fi
