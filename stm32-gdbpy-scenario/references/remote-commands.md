# Команды цели без GDB: стоп, старт, сброс и чтение одной строкой

Проверено 28.10.2026 на шести стендах: BlackPill F411CE локально (Windows, OpenOCD 0.12.0)
и на узле Orange Pi 5 по SSH (xPack OpenOCD 0.12.0+dev) — F411CE, F401CC, F103C8, F030R8
(Nucleo, ST-Link V2-1) и F429ZI (ST-Link V2 с нечитаемым серийником, выбор по USB-адресу).

## 1. Одноразовый сеанс OpenOCD

Каждый вызов: `init` → команды → `shutdown`. GDB не нужен, ядро для чтения памяти не
останавливается (порт отладки читает шину во время работы).

```sh
# чтение адреса одной строкой на узле стенда
ssh user@stand "openocd -f interface/stlink.cfg -c 'adapter serial <серийник>' -f target/stm32f4x.cfg \
  -c init -c 'mdw 0xE0042000' -c shutdown 2>&1 | grep -i '^0x'"
# 0xe0042000: 10006431
```

То же через помощник навыка (выбор пробника, разбор ответа, несколько действий за сеанс):

```sh
python scripts/ocd.py --ssh user@stand --openocd <путь к openocd на узле> --target stm32f4x \
  --serial <серийник> state read 0x20000028 read 0x2000002C 2
# state RUNNING (DHCSR 0x01010001)
# 0x20000028 0x000000A5
# 0x2000002C 0x0004EDD0
# 0x20000030 0x00000011
```

| Действие `ocd.py` | Команды OpenOCD | Что проверено |
| :--- | :--- | :--- |
| `state` | `mdw 0xE000EDF0` (DHCSR) | RUNNING, SLEEPING (WFI), HALTED, LOCKUP |
| `read ADDR [N]` | `mdw ADDR N` | счётчик прошивки растёт между чтениями: ядро не останавливается |
| `halt` | `halt` | **остаётся остановленным после выхода OpenOCD** |
| `resume` | `halt`, `resume` | без `halt` в новом сеансе: `not halted ... context restore failed` |
| `reset-run` | `reset run` | ядро снова работает с начала прошивки |
| `reset-halt` | `reset halt` | PC = вектор сброса из `0x08000004` |
| `raw "…"` | как есть | `flash read_bank`, `program … verify reset`, `reg pc` |

Время одного вызова: локально 0,3 с, через SSH к узлу 0,7–1,9 с (запуск OpenOCD и `init`).

### Почему `resume` нужен вместе с `halt`

Сразу после `init` OpenOCD ещё не опросил цель и считает её не остановленной, даже если
ядро стоит после прошлого сеанса. `resume` в таком сеансе отклоняется. Работают
`halt` + `resume` (для работающего ядра — короткая остановка), `wait_halt 100` + `resume`
или `poll` + пауза + `resume`; `targets` + `resume` не помогает.

### Остановка сохраняется

`halt` в одноразовом сеансе оставляет ядро остановленным: DHCSR `0x00030003`, счётчик
SysTick стоит. Каждый `halt` должен заканчиваться `resume` или `reset-run` — иначе
следующий опыт увидит «застывшие» значения.

## 2. Работающий OpenOCD и Tcl-порт

Если команд много, сервер запускается один раз, а команды идут через Tcl-порт: ответ
заканчивается байтом `0x1a`, команда оборачивается в `capture {…}`.

```sh
# сервер на узле: только 127.0.0.1, без GDB и telnet
ssh user@stand "nohup openocd -f interface/stlink.cfg -c 'adapter serial <серийник>' -f target/stm32f4x.cfg \
  -c 'bindto 127.0.0.1' -c 'gdb_port disabled' -c 'telnet_port disabled' -c 'tcl_port 6666' \
  > /tmp/ocd.log 2>&1 < /dev/null & echo \$! > /tmp/ocd.pid"
# чтение одной строкой: socat ждёт ответ 0,3 с после конца ввода
ssh user@stand "printf 'capture {mdw 0xE0042000}\032' | socat -t 0.3 - TCP:127.0.0.1:6666"
# остановить свой сервер
ssh user@stand "kill \$(cat /tmp/ocd.pid)"
```

| Способ | Время с хоста | Примечание |
| :--- | :--- | :--- |
| `socat -t 0.3` на узле | ~0,5 с | быстрее всего одной командой SSH |
| `nc -q 1` на узле | ~1,5 с | `nc` ждёт секунду после конца ввода |
| SSH-туннель + `ocd.py --tcl` | ~0,4 с на вызов | туннель держится открытым, команды без SSH на каждый вызов |

```sh
ssh -N -o ExitOnForwardFailure=yes -L <локальный порт>:127.0.0.1:6666 user@stand &
python scripts/ocd.py --tcl 127.0.0.1:<локальный порт> halt state resume state
```

## 3. Выбор пробника

- Серийник: `adapter serial <серийник>` — после `-f interface/...`, до `-f target/...`.
- ST-Link V2 со старой прошивкой отдаёт серийник из непечатаемых байтов; тогда пробник
  выбирается по USB-адресу: `adapter usb location <bus>-<port[.port]>` (Linux:
  `echo $(cat $d/busnum)-$(cat $d/devpath)` для каталога устройства в `/sys/bus/usb/devices`).
  Адрес меняется при переподключении в другой разъём — сверять перед работой.
- Тип МК после выбора — по DEV_ID (`read 0xE0042000`, у F0 — `0x40015800`).

## 4. Копия Flash и возврат

```sh
python scripts/ocd.py ... reset-halt raw "flash read_bank 0 /tmp/backup.bin" reset-run
python scripts/ocd.py ... raw "program build/app.elf verify reset"           # своя прошивка
python scripts/ocd.py ... raw "program /tmp/backup.bin 0x08000000 verify reset"   # возврат
python scripts/ocd.py ... raw "flash read_bank 0 /tmp/check.bin"            # сверить хеш с копией
```

Чтение банка: 512 КиБ за 5 с локально, 256 КиБ за 2,6 с на узле. Возврат 512 КиБ — 13 с.
После прошивки готовность проверяется переменной прошивки (например, `probe_state = 0xA5`),
а не паузой. Копия — файл с серийным номером стенда в имени не коммитится.

## 5. Ловушки

- **Windows: `couldn't bind tcl to socket`**. Фиксированный порт попал в диапазон,
  зарезервированный системой (Hyper-V, `netsh int ipv4 show excludedportrange protocol=tcp`).
  Брать порт, выданный системой (`bind(("127.0.0.1", 0))`), как делает запускатель.
- Порядок вывода: эхо-маркеры и ответы OpenOCD идут в общий поток только при `2>&1`
  (stdout и stderr вперемешку теряют порядок).
- `~` в пути к OpenOCD на узле не раскрывается в кавычках: `"$HOME"/.local/...`.
- Чтение памяти не останавливает ядро, но `halt`, `reset` и прошивка — вмешательство:
  на силовой плате выходы зонда или сброс переключают ключи. Сначала убедиться, что плата тестовая.
