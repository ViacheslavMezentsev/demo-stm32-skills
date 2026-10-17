---
name: stm32-rtt-console
description: 'Real-Time Transfer (RTT) через OpenOCD и ST-Link: быстрый двусторонний канал обмена с прошивкой поверх SWD, без занятия UART и с сохранением скорости отладки. Команды rtt setup/start/server, проверка совместимости (поддержка в OpenOCD, буферы в прошивке, отсутствие конфликта с занятым отладчиком) и требования к проекту перед включением.'
license: BSD-3-Clause
compatibility: STM32 с SWD-отладчиком и OpenOCD 0.11+ (у нас 0.12.0+dev); управляющий блок RTT в памяти цели либо инъекция блока в свободную SRAM
metadata:
  version: 1.0.0
  workflow: исследование → навык → проект
allowed-tools: Bash Read Write Edit Glob Grep
---

# RTT через OpenOCD: быстрый канал обмена с прошивкой

Применяй, когда нужно: получать журнал из прошивки **быстрее и надёжнее UART**; иметь **двусторонний**
канал (команды внутрь, ответы наружу); разгрузить UART под прикладной протокол; читать журнал, не
занимая последовательный порт, который нужен другому потребителю.

**Статус в проекте: не используется.** В исходниках нет поддержки RTT, встраивание в прошивку не
делалось. **Но проверено (исследование 17):** RTT работает и **без изменения прошивки** — управляющий
блок можно создать в свободной SRAM инъекцией отладчика, OpenOCD найдёт его по строке `SEGGER RTT`.
Ограничение: без поставщика данных канал пуст — сначала должен быть поднят блок RTT в памяти цели.

## 0. Готовый PoC (если нужно «здесь и сейчас»)

```sh
# Вариант A: пассивный блок — канал наполняет отладчик
python scripts/build_block.py --out build/rtt-block.bin
python scripts/run_poc.py       # инъекция + rtt setup/start/channels + чтение
python scripts/run_live.py      # живой поток: отладчик пишет, сервер читает

# Вариант B: перехват вектора через VTOR — данные пишет ЗАГЛУШКА в SRAM, поток без искажений
python scripts/run_vector_hook.py --vector SysTick --seconds 6

# Вариант C: произвольная функция через аппаратную точку останова (медленно, для редких событий)
python scripts/run_bp_hook.py --symbol <symbol> --seconds 10

# Вариант D: РЕЗУЛЬТАТ функции через подмену lr (тот же класс цены, но даёт возвращаемое значение)
python scripts/run_retval_hook.py --symbol <symbol> --seconds 10
```

**Выбор варианта (замерено 17.10.2026):**

| Нужно | Механизм | Цена |
|

## Файлы навыка

```
scripts\build_block.py
scripts\check_rtt_support.sh
scripts\hook\build_hook_image.py
scripts\hook\hook.S
scripts\hook\retval_hook.S
scripts\hook\run_bp_hook.py
scripts\hook\run_hook.py
scripts\hook\run_retval_hook.py
scripts\hook\run_timer_hook.py
scripts\hook\run_vector_hook.py
scripts\hook\timer_hook.S
scripts\hook\vector_hook.S
scripts\rtt_help.sh
scripts\rtt_read.sh
scripts\run_live.py
scripts\run_poc.py
scripts\run_running.py
scripts\start_openocd.sh
```
