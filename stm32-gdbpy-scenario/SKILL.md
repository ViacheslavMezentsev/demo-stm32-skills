---
name: stm32-gdbpy-scenario
description: 'Запуск простого сценария или короткой последовательности действий на STM32 (и других Cortex-M) через GDB с Python (gdb-py) без специальных фреймворков: маршрут Renode, ST-Link или J-Link через OpenOCD, J-Link GDB Server, локально или на удалённом стенде через SSH-туннель, подготовительные проверки (GDB с Python, отпечаток пробника и МК, работающее ядро, таймауты), шаблон сценария с check/reach/write и восстановлением, запускатель с бюджетом времени и уборкой своих процессов, итог PASS/FAIL/ERROR/TIMEOUT в JSON; команды цели без GDB (состояние, стоп, старт, сброс, чтение адреса одной строкой через SSH, копия и возврат Flash). Использовать, когда нужно дойти до функции, прочитать или временно изменить переменную, проверить реакцию прошивки и оставить плату в прежнем состоянии; когда сценарий зависает, GDB не выполняет Python или результат проверяется по управляющему сигналу вместо наблюдаемого.'
license: MIT
compatibility: Python 3.9+, GDB с Python (arm-none-eabi-gdb-py3 из xPack/Arm GNU Toolchain или gdb-multiarch), для эмуляции Renode 1.17, для платы OpenOCD 0.11+ с ST-Link или J-Link (SWD) либо SEGGER J-Link Software, для удалённого стенда SSH-клиент. Windows и Linux.
metadata:
  author: Viacheslav Mezentsev
  version: "1.2.0"
  entrypoint: scripts/run_scenario.py
  related: stm32-renode-emulator stm32-diag-record stm32-ros-alt-channel
allowed-tools: Bash Read Write Edit Glob Grep
---

# Сценарий gdb-py на STM32

Один сценарий — один прогон — один проверяемый итог. Навык описывает, как выполнить
действие через Python внутри GDB (gdb-py) и доказать результат: дойти до функции,
прочитать или временно изменить переменную, проверить наблюдаемую реакцию прошивки и
вернуть всё как было. Нужны только GDB с Python, GDB-сервер (Renode или OpenOCD) и
Python на хосте; посторонние пакеты не требуются.

**Главное правило: подготовка и проверка важнее действия.** Почти все провалы случаются
до воздействия (не тот GDB, не та плата, ядро стоит) или после него, когда успех
засчитывается по управляющему сигналу («записали», «ключ включён»), а не по наблюдаемому.

Состав навыка:

```
scripts/run_scenario.py        запускатель: сервер (локально или на узле по SSH), GDB, бюджет, run.json, уборка
scripts/ocd.py                 команды без GDB через OpenOCD: state, read, halt, resume, reset-run, reset-halt, raw
scripts/jlink.py               те же команды через SEGGER J-Link Commander (JLinkExe)
assets/scenario_template.py    шаблон сценария: check/reach/read/write/monitor/record, восстановление
assets/probe_scenario.py       пример для прошивки-зонда stm32-skills-probe (Renode и платы)
assets/address_scenario.py     пример без ELF: DEV_ID, остановка, сброс на вектор, stepi
references/remote-commands.md  стоп, старт, сброс, чтение одной строкой, Tcl-порт, копия Flash
references/pitfalls.md         провалы с симптомами, причинами и правильными приёмами
```

## 1. Маршрут: выбрать до любых команд

| Маршрут | Когда | Как запускать |
| :--- | :--- | :--- |
| `emulator` | платы нет или нужен воспроизводимый прогон | `--renode-platform <модель.repl>`: Renode с GDB-сервером на свободном порту |
| `local` | ST-Link в USB этой машины | `--openocd-config interface/stlink.cfg --openocd-config target/<серия>.cfg` |
| `jlink` | J-Link, сервер SEGGER | `--jlink-device <имя МК в базе SEGGER> --adapter-serial <номер J-Link>` (локально или с `--ssh`) |
| `remote` | пробник на узле стенда, доступ по SSH | `--ssh user@host --openocd <путь на узле> --openocd-config ...`: OpenOCD на узле и туннель поднимает запускатель |

PASS в эмуляторе подтверждает только поведение в модели; аппаратная проверка — отдельный
прогон на плате. Запускатель пишет окружение (`emulator`/`hardware`) в итог, а сценарий
обязан записать то же значение.

### 1.1 Отпечаток маршрута

Для платы до действий фиксируется тройка `маршрут | серийник пробника | узел` и сверяется
с прошлыми прогонами. Изменилась — остановиться: прежние наблюдения относятся к другому стенду.

```sh
# Linux: пробники ST в USB (так же на узле стенда по SSH)
for d in /sys/bus/usb/devices/*; do [ -f "$d/idVendor" ] && [ "$(cat $d/idVendor)" = 0483 ] && \
  echo "$(cat $d/serial) $(cat $d/product)"; done
```

Если пробников несколько, серийник передаётся явно: `--adapter-serial <серийник>`. ST-Link V2 со
старой прошивкой отдаёт нечитаемый серийник — тогда `--adapter-usb-location <bus>-<port>`.
Серийные номера и адреса стендов не коммитятся: они живут в локальных файлах стенда.

### 1.2 Тип МК

`DEV_ID` — младшие 12 бит `DBGMCU_IDCODE`. Сверить с профилем до действия:

| Серия | Адрес IDCODE | DEV_ID (примеры) |
| :--- | :--- | :--- |
| STM32F0 | `0x40015800` | `0x440` F030x8 |
| STM32F1 | `0xE0042000` | `0x410` medium density (F103C8/CB), `0x414` high density |
| STM32F4 | `0xE0042000` | `0x423` F401xB/C, `0x433` F401xD/E, `0x431` F411, `0x419` F42x/F43x |
| STM32G4 | `0xE0042000` | `0x468` G431, `0x469` G47x/G48x |

Значения сверяйте с Reference Manual своей серии. В сценарии:
`check("DEV_ID", read("*(unsigned int*)0xE0042000") & 0xFFF == 0x431)`. Совместимые МК
других производителей могут отвечать другим кодом — тогда профиль задаёт свой.

## 2. Подготовка до действия

### 2.1 GDB с Python — первым

```sh
python scripts/run_scenario.py --check-gdb
```

Скрипт ищет `--gdb`, затем `GDB`, `ARM_TOOLCHAIN_ROOT`/`STM32_TOOLCHAIN_PATH` и `PATH`
(`arm-none-eabi-gdb-py3`, `arm-none-eabi-gdb`, `gdb-multiarch`) и принимает только GDB,
который выполнил `python print(6 * 7)`. **`arm-none-eabi-gdb` без `-py3` в xPack собран без
Python**: Python-файл исполняется как команды GDB и падает на первой строке.

### 2.2 Символы и адреса — из ELF

Сценарий обращается к переменным и функциям по именам из DWARF того же ELF, что прошит.
Адреса из старых заметок не используются: они меняются с каждой сборкой.

```sh
arm-none-eabi-gdb-py3 -nx -batch build/app.elf -ex "p &uwTick" -ex "ptype g_app"
```

### 2.3 Состояние цели — три вопроса

1. **Ядро работает?** Счётчик тиков (`uwTick` в HAL, свой счётчик в зонде) меняется
   между двумя остановками. Не меняется — цель в halt или в HardFault, действие запрещено.
2. **В каком состоянии логика?** Часть воздействий допустима только в определённом
   состоянии автомата; проверить до действия, а не после.
3. **Что сейчас на выходах?** Прочитать регистр или переменную, а не полагаться на память.

### 2.4 Таймауты — назначить до запуска

| Что ограничиваем | Значение | Чем |
| :--- | :--- | :--- |
| Ответ GDB-сервера | 10–20 с | `set remotetimeout 15` в шаблоне |
| Прогон GDB целиком | 30–60 с на короткий сценарий | `--timeout` запускателя; по истечении — TIMEOUT |
| Готовность сервера | 30 с | запускатель ждёт строку журнала Renode/OpenOCD |
| Ожидание события | по тикам цели, а не по часам хоста | счётчик прошивки в сценарии |
| Повторы в цикле | жёсткий потолок итераций | `for _ in range(N)` вместо `while True` |
| SSH до узла | 10 с | `-o ConnectTimeout=10` |

## 3. Сценарий

### 3.1 Шаблон

Скопируйте `assets/scenario_template.py` и меняйте только раздел «Сценарий». Помощники:

| Функция | Что делает |
| :--- | :--- |
| `step(name)` | печатает `STEP name`; при TIMEOUT запускатель покажет последний шаг |
| `check(name, value)` | записывает проверку и печатает `PASS`/`FAIL name` |
| `reach(symbol, condition=None)` | временная точка останова, `continue`, проверка кадра |
| `read(expr)` | целое значение выражения C в текущем кадре |
| `write(expr, value)` | `set var`; исходное значение запоминается один раз |
| `monitor(command)` | команда GDB-сервера (`reset halt`) и сброс кэша регистров GDB |
| `record(name, data)` | наблюдение в итог (сырые числа, а не только вердикт) |
| `restore()` | возврат записанного и проверка чтением; вызывается в `finally` |

Порядок внутри раздела всегда одинаковый:

1. исходное состояние (прочитать то, что будет сравниваться);
2. действие (одна запись, вызов или достижение точки);
3. наблюдаемый результат — следствие, ради которого действие делалось;
4. соседний признак — то, что меняться не должно, не изменилось.

Итог пишется всегда, в `finally`: `status` (`PASS`, если все проверки прошли, `FAIL` при
провале проверки, `ERROR` при исключении или неподтверждённом восстановлении), `checks`,
`records`, `restored`, `environment`. Затем `quit 0` только для PASS.

### 3.2 Правила сценария

- API GDB вызывается только из основного потока GDB: без потоков и `asyncio` внутри сценария.
- Условие точки — атрибутом `point.condition = "..."`; конструктор `gdb.Breakpoint` в части
  сборок GDB не принимает `condition`.
- Перед каждым `continue` есть точка останова, иначе GDB ждёт остановки бесконечно.
- Запись в переменную — не её потребление. Доказательство — реакция прошивки
  (пример: новый период кадров действует только со следующего запланированного кадра).
- Всё записанное возвращается и проверяется чтением; при неудаче итог `ERROR`.
- На плате GDB подключается к работающей прошивке: `main` уже пройден. Сценарий от начала
  начинается с `monitor("reset halt")` (так в шаблоне для `hardware`), иначе `reach("main")` висит.
- Без ELF сценарий работает по адресам и регистрам (`assets/address_scenario.py`): `--elf` не нужен.
- На плате в конце `continue &`, затем `detach`: `detach` при остановленном ядре оставляет
  цель в halt, и следующий прогон начинается со «застывших» счётчиков.

## 4. Запуск

```sh
# эмулятор
python scripts/run_scenario.py --elf build/app.elf --scenario tests/my_scenario.py \
    --renode-platform emu/renode/board.repl --out build/scenario/run1

# локальная плата
python scripts/run_scenario.py --elf build/app.elf --scenario tests/my_scenario.py \
    --openocd-config interface/stlink.cfg --openocd-config target/stm32f4x.cfg --out build/scenario/run2

# удалённый стенд: OpenOCD на узле и туннель поднимает запускатель (раздел 4.1)
python scripts/run_scenario.py --elf build/app.elf --scenario tests/my_scenario.py \
    --ssh user@stand --ssh-key ~/.ssh/id_stand --openocd '~/.local/openocd/bin/openocd' \
    --openocd-config interface/stlink.cfg --openocd-config target/stm32f4x.cfg \
    --adapter-serial <серийник> --out build/scenario/run3

# готовый сервер или свой туннель
python scripts/run_scenario.py --scenario assets/address_scenario.py --attach 127.0.0.1:13333 \
    --environment hardware --out build/scenario/run4
```

Запускатель:

- проверяет GDB с Python и не принимает непустой каталог прогона;
- поднимает сервер в отдельной группе процессов на свободном порту `127.0.0.1` и ждёт
  его готовности по журналу (пробное TCP-подключение заняло бы единственный GDB-сеанс);
- запускает `gdb -nx -batch <elf> -ex "source <сценарий>"` списком аргументов, без оболочки;
- по истечении `--timeout` останавливает дерево процессов GDB и пишет `TIMEOUT` с последним шагом;
- сверяет итог сценария с кодом выхода GDB и окружением; несогласие — `ERROR`;
- останавливает только свои процессы и пишет `run.json`, `gdb.log`, `server.log`.

Код возврата: 0 — PASS, 1 — FAIL, 2 — ERROR или TIMEOUT. Модель Renode с дополнительными
строками монитора: `--renode-line "sysbus.cpu VectorTableOffset 0x08000000"` (повторяемый ключ).

В PowerShell GDB вручную вызывается оператором `&` с массивом аргументов. `Start-Process
-ArgumentList` пересобирает аргументы в строку, и `-ex "source file"` разваливается на два.

### 4.1 Удалённый стенд

С `--ssh` запускатель сам выполняет то, что иначе делается вручную:

1. на узле выбирает свободный порт и запускает OpenOCD через `nohup` (только `127.0.0.1`,
   без telnet и Tcl), ждёт строку готовности в журнале и запоминает pid;
2. поднимает `ssh -N -L` с `ExitOnForwardFailure=yes` и `ConnectTimeout=10` на свободный
   локальный порт (готовность — живой процесс, без пробного подключения к GDB-порту);
3. после сценария останавливает туннель, убивает на узле только свой pid и забирает журнал
   OpenOCD в `server.log`.

Для ручной работы: сложные команды передаются файлом (`scp`, затем `ssh host "bash /tmp/job.sh"`),
файл `.sh` — с окончаниями LF; `~` в пути на узле в кавычках не раскрывается (`"$HOME"/...`).

### 4.2 Команды цели без GDB

Стоп, старт, сброс и чтение памяти не требуют GDB: `scripts/ocd.py` запускает OpenOCD на один
сеанс (локально или на узле по SSH) либо шлёт команды работающему OpenOCD через Tcl-порт.

```sh
python scripts/ocd.py --ssh user@stand --openocd '<openocd на узле>' --target stm32f4x \
    --serial <серийник> state read 0xE0042000          # state RUNNING ... / 0xE0042000 0x10006431
python scripts/ocd.py ... halt state                    # остаётся HALTED и после выхода OpenOCD
python scripts/ocd.py ... resume state                  # в новом сеансе — halt + resume
python scripts/ocd.py ... reset-halt raw "reg pc"       # PC на векторе сброса
python scripts/ocd.py ... reset-run state
```

Чтение одной строкой без помощника:

```sh
ssh user@stand "openocd -f interface/stlink.cfg -c 'adapter serial <серийник>' -f target/stm32f4x.cfg \
  -c init -c 'mdw 0xE0042000' -c shutdown 2>&1 | grep -i '^0x'"
```

Что установлено на стендах (подробно — [references/remote-commands.md](references/remote-commands.md)):

- чтение памяти не останавливает ядро: счётчик прошивки растёт между чтениями;
- `halt` в одноразовом сеансе сохраняется после выхода OpenOCD — его надо снять;
- `resume` сразу после `init` отклоняется (`not halted`): OpenOCD ещё не опросил цель;
  работает `halt` + `resume`;
- вызов: 0,3 с локально, 0,7–1,9 с по SSH; с работающим сервером — `socat` на узле ~0,5 с,
  туннель и `--tcl` ~0,4 с, локально 0,1 с;
- копия Flash (`flash read_bank`), прошивка и возврат образа с проверкой хеша — `raw`.

### 4.3 J-Link: отличия от ST-Link

| Что | J-Link GDB Server | OpenOCD + `interface/jlink.cfg` | ST-Link + OpenOCD |
| :--- | :--- | :--- | :--- |
| Выбор пробника | `-USB <десятичный номер>` | `adapter serial <номер>` | `adapter serial <серийник>` |
| Транспорт | `-if SWD` | **`transport select swd`** (иначе JTAG) | выбирает сам (hla) |
| МК | `-device <имя SEGGER>` | конфиг цели | конфиг цели |
| Сброс с остановкой | `monitor reset` | `monitor reset halt` | `monitor reset halt` |
| После подключения | `monitor flash breakpoints = 0` | — | — |
| Выход из сценария | `monitor go`, `disconnect` | `continue &`, `detach` | `continue &`, `detach` |

Запускатель передаёт сценарию команду сброса, подготовку и выход сервера через
`SCENARIO_RESET_HALT`, `SCENARIO_SETUP` и `SCENARIO_FINISH`; шаблон их применяет.
`monitor flash breakpoints = 0` обязателен: иначе J-Link GDB Server ставит программные точки
останова записью во Flash. Те же приёмы использует модуль stm32-gdbtest (backend `jlink`).

```sh
# J-Link GDB Server на узле
python scripts/run_scenario.py --scenario assets/address_scenario.py --jlink-device AT32F403ACGU7 \
    --jlink-gdbserver /opt/SEGGER/JLink/JLinkGDBServerCLExe --adapter-serial <номер> \
    --ssh user@stand --out build/scenario/jlink
# J-Link через OpenOCD (AT32: драйвер Flash at32f4x не опознал AT32F403A — карта памяти отключена)
python scripts/run_scenario.py --scenario assets/address_scenario.py --openocd-config interface/jlink.cfg \
    --openocd-transport swd --openocd-set CPUTAPID=0x2ba01477 --openocd-config target/artery/at32f4x.cfg \
    --openocd-command "gdb_memory_map disable" --openocd-command "gdb_breakpoint_override hard" \
    --adapter-serial <номер> --ssh user@stand --openocd '<openocd на узле>' --out build/scenario/ocd-jlink
# команды без GDB
python scripts/jlink.py --ssh user@stand --jlinkexe /opt/SEGGER/JLink/JLinkExe --device AT32F403ACGU7 \
    --serial <номер> state read 0xE0042000 halt state resume
python scripts/ocd.py --ssh user@stand --openocd '<openocd на узле>' --interface interface/jlink.cfg \
    --transport swd --serial <номер> --set CPUTAPID=0x2ba01477 --target artery/at32f4x state
```

J-Link Commander при выходе отпускает ядро: `halt` действует только внутри одного вызова
`jlink.py`, а `resume` для работающего ядра отвечает «already running». У OpenOCD, наоборот,
`halt` сохраняется после выхода.

## 5. Действия, которые не завершаются сами

| Действие | Почему висит | Ограничение |
| :--- | :--- | :--- |
| `continue` без точки останова | цель работает, остановки не будет | точка перед каждым `continue` |
| `reach` с условием, которое не наступит | при HardFault или не том состоянии — никогда | внешний `--timeout`, проверка состояния до действия |
| Цикл ожидания без потолка | событие может не наступить вовсе | потолок итераций и окно по тикам цели |
| Ожидание по `time.sleep` хоста | остановки отладчика растягивают время цели | тики прошивки |
| SSH без `ConnectTimeout` | недоступный узел держит минуты | `-o ConnectTimeout=10` |
| GDB без внешнего бюджета | любая строка выше останавливает прогон | `--timeout` запускателя |

Признак ловушки: нет вывода дольше 30 с. Прервать, проверить тики и состояние цели,
восстановить её, только потом повторять. В эмуляторе прошивка, завершающая себя
семихостингом, останавливает ядро: `reach` тогда возвращается с чужим кадром и даёт FAIL, а не зависание.

## 6. Проверка результата

**Проверять наблюдаемое, а не управляющее.** Ключ питания показывает намерение прошивки,
работу тракта — только трафик; «событие подставлено» не значит «обработано».

| Что проверяем | Чем | Нельзя подменять |
| :--- | :--- | :--- |
| Обработчик принял значение | изменённое поведение (период, счётчик, состояние) | чтением только что записанного |
| Линия работает | рост счётчика пакетов за окно | уровнем ключа |
| Кнопка сработала | изменился целевой признак | фактом записи в регистр ввода |
| Ничего лишнего | соседние признаки прежние | отсутствием ошибок GDB |

После действия: тики растут, состояние автомата ожидаемое, целевой признак изменился,
соседние не изменились, записанное возвращено.

## 7. Восстановление и уборка

| Симптом | Признак | Действие |
| :--- | :--- | :--- |
| Цель в halt | тики не меняются | `continue &` и `detach`; через telnet OpenOCD — `resume` |
| HardFault | кадр `HardFault_Handler`, регистры не читаются | `reset run`; помнить, что сброс стирает данные в SRAM |
| Порт или пробник занят | OpenOCD не стартует, `address in use` | найти свой процесс; чужой отладчик не трогать |
| Узел недоступен | SSH не отвечает | сообщить, действия не выполнять |

Запускатель убирает только запущенные им сервер и GDB. Туннели и OpenOCD, поднятые
вручную, останавливаются по сохранённому pid; `pkill -f '[o]penocd'` на узле — только
если OpenOCD там запущен вами (скобки не дают шаблону совпасть с собственной оболочкой).
Освобождение порта проверяется реальным подключением, а не списком процессов.

## 8. Примеры и проверенные стенды

`assets/probe_scenario.py` выполняется на [stm32-skills-probe](https://github.com/ViacheslavMezentsev/stm32-skills-probe)
в Renode (модель `emu/renode/stm32f4_probe.repl` с UART семихостинга) и на платах:

1. до инициализации `probe_state != 0xA5`, после `probe_init` — `0xA5`;
2. два кадра подряд: период 100 мс по `probe_telemetry.tick`;
3. действие: `probe_frame_period_ms = 20`;
4. следующий интервал остаётся 100 мс (кадр уже запланирован), за ним — 20 мс;
5. соседние признаки прежние, период возвращён и проверен чтением.

`assets/address_scenario.py` без ELF: DEV_ID (`SCENARIO_DEV_ID`, `SCENARIO_IDCODE`), остановка при
подключении, `reset halt` на вектор сброса, `stepi`.

```sh
cmake -S stm32-skills-probe -B build/probe -G Ninja -DBOARD=F411CE -DARM_TOOLCHAIN_ROOT=<тулчейн>
cmake --build build/probe        # для Renode добавить -DPROBE_LOG_SEMIHOSTING=ON -DPROBE_EXIT_MS=500
```

Проверено 28.10.2026 (OpenOCD 0.12, SEGGER J-Link 9.80, `arm-none-eabi-gdb-py3` 15.2):

| Стенд | Маршрут | Команды и чтение | Сценарий по адресам | Зонд: Flash → сценарий → возврат |
| :--- | :--- | :--- | :--- | :--- |
| BlackPill F411CE | локально, Windows | PASS | PASS | PASS, хеш образа совпал |
| BlackPill F411CE | узел по SSH | PASS | PASS | PASS, хеш образа совпал |
| BlackPill F401CC | узел по SSH | PASS | PASS | PASS, хеш образа совпал |
| Blue Pill F103C8 | узел по SSH | PASS | PASS | PASS, хеш образа совпал |
| Nucleo F030R8 | узел по SSH | PASS | PASS | — (зонд не поддерживает) |
| F429ZI, ST-Link V2 | узел по SSH, USB-адрес | PASS | PASS | — (зонд не поддерживает) |
| AT32F403A, J-Link | узел по SSH: JLinkExe и OpenOCD | PASS | PASS (J-Link GDB Server и OpenOCD) | — (зонд не поддерживает) |

В CI репозитория пример зонда и четыре варианта шаблона (PASS, FAIL, ERROR с возвратом
переменной, TIMEOUT с последним шагом) выполняются в Renode. Однажды на трёх удалённых платах
первое чтение сразу после прошивки вернуло нули при работающем ядре; в повторах готовность
была с первой попытки. Причина не установлена, поэтому готовность проверяется переменной прошивки.

## 9. Порядок целиком

1. Выбрать маршрут; для платы — сверить отпечаток пробника и узла.
2. `run_scenario.py --check-gdb`.
3. Взять имена и типы из ELF прошиваемой сборки; сверить DEV_ID.
4. Проверить, что ядро работает и логика в нужном состоянии.
5. Назначить таймауты и потолки итераций.
6. Написать сценарий по шаблону: исходное → действие → наблюдаемое → соседнее.
7. Запустить; разобрать `run.json`, при TIMEOUT — `last_step` и `gdb.log`.
8. Убедиться, что записанное возвращено, а плата работает.
9. Остановить вручную поднятые туннели и серверы.

## Границы

- Одно действие или короткая последовательность. Наборы сценариев, отчёты, карты
  требований и повторные кампании — задача тестового фреймворка (например, stm32-gdbtest),
  а не этого навыка; формат итога здесь намеренно простой и локальный.
- Навык не подтверждает требования сам: он выполняет действие и проверяет наблюдаемые величины.
- Точные времена фронтов и ШИМ — логическим анализатором (weact-logic-analyzer), не опросом по SWD.
- Длительную запись переменных — stm32-diag-record или stm32-ros-alt-channel.
- Провалы с командами и симптомами: [references/pitfalls.md](references/pitfalls.md).
