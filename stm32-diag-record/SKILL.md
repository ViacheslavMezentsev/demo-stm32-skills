---
name: stm32-diag-record
description: 'Запись диагностических полей прошивки STM32 и других Cortex-M в файл mcap или bag без ROS: один скрипт поднимает OpenOCD (без сброса цели), читает переменные, поля структур и регистры периферии по SWD, пока прошивка работает, и сохраняет файл заданной длительности; без платы те же поля читаются из работающего Renode через его монитор; адреса считаются офлайн по ELF и SVD. Использовать, когда нужно снять значения с платы или эмулятора для офлайн-разбора в PlotJuggler или mcap/rosbag, в том числе из задачи VS Code, без установки ROS и WSL.'
license: MIT
compatibility: Windows или Linux с Python 3.9+ (3.11+ или пакет tomli для resolve_addresses.py), OpenOCD 0.11+ и SWD-пробник, который он поддерживает (ST-Link, CMSIS-DAP, J-Link и др.) или Renode 1.15+ без платы, GDB с Python для адресов по ELF, SVD-файл для полей svd:; ROS не требуется
metadata:
  author: Viacheslav Mezentsev
  version: "1.3.0"
  kind: diagnostic-recorder
  outputs: mcap, rosbag-ros1
  entrypoint: scripts/record_diag.py
  analog: stm32-ros-alt-channel
allowed-tools: Bash Read Write Edit Glob Grep
---

# Запись диагностики с платы без ROS

Один скрипт: поднимает OpenOCD, читает выбранные поля прошивки по SWD и пишет файл `mcap` или `bag`.
Проверен на STM32, но к STM32 не привязан: подходит Cortex-M, с которым работает OpenOCD (раздел 5).
ROS не нужен ни для записи, ни для чтения. Если нужны живые топики в ROS-графе, — навык
`stm32-ros-alt-channel`; общие модули у навыков одинаковые (см. «Файлы навыка»).

## 1. Что происходит

```
ELF (+SVD) -> resolve_addresses.py -> адреса полей            (офлайн, плата не нужна)
плата (RAM) -> SWD -> OpenOCD (telnet) -> mdw по адресам -> сериализация std_msgs -> mcap | bag
Renode      -> монитор (telnet, --port) -> sysbus ReadDoubleWord  -> то же              (--backend renode)
```

OpenOCD подключается **без сброса**: прошивка продолжает работать с того места, где была (`--reset` —
только если сброс нужен). Ядро не останавливается, UART не занимается. Соседние адреса (в окне 256 байт)
читаются одной командой `mdw`, невыровненное поле собирается из двух слов.

## 2. Команды

```sh
python scripts/record_diag.py --doctor                  # совместимость: пробник, цель, адреса, чтение на ходу
python scripts/record_diag.py --dryrun --seconds 10     # план и команда OpenOCD, ничего не запускается
python scripts/record_diag.py --record mcap --seconds 30
python scripts/record_diag.py --record bag --compress bz2 --seconds 30 --out diag.bag
python scripts/record_diag.py --target target/stm32f1x.cfg --serial <серийный номер> --record mcap
python scripts/record_diag.py --use-running --port 4444 --record mcap   # OpenOCD уже запущен
python scripts/record_diag.py --backend renode --port 1234 --record mcap --seconds 10   # без платы
```

| Ключ | Смысл |
|---|---|
| `--record mcap\|bag` (он же `--format`) | формат файла; по умолчанию `mcap` |
| `--out ПУТЬ` | файл записи; по умолчанию `<каталог ELF>/diag/record-<дата>.<формат>` |
| `--seconds N`, `--rate ГЦ` | длительность (10 с) и частота опроса (10 Гц) |
| `--fields ПУТЬ` | спецификация полей (TOML), по умолчанию `scripts/fields.toml` |
| `--elf ПУТЬ` | ELF; иначе `$FIRMWARE_ELF`, иначе первый `build/*/*.elf` |
| `--svd ПУТЬ` | SVD для полей `svd:...`; иначе `$STM32_SVD` |
| `--interface`, `--target` | конфигурации OpenOCD: пробник (`interface/stlink.cfg`, `cmsis-dap.cfg`, `jlink.cfg`) и цель (`target/stm32f4x.cfg`, `stm32f1x.cfg`, `nrf52.cfg`...) или путь к своему `.cfg` |
| `--openocd-command CMD` | команда OpenOCD после конфигурации цели, до `init` (`adapter speed 4000`, `reset_config none`); можно несколько раз |
| `--serial SN` | серийный номер пробника; иначе `$STLINK_SERIAL`, иначе первый найденный |
| `--reset` | сбросить цель после подключения |
| `--compress none\|bz2` | сжатие чанка bag |
| `--chunk-size N` | сообщений на чанк mcap |
| `--use-running`, `--host`, `--port` | подключиться к уже запущенному OpenOCD (порт 4444) |
| `--backend openocd\|renode` | источник: OpenOCD и плата (по умолчанию) или монитор Renode (порт 1234) |
| `--machine ИМЯ` | Renode: машина, если их в мониторе несколько |
| `--addresses ПУТЬ` | куда сохранить JSON адресов (по умолчанию рядом с записью) |

После записи скрипт печатает размер файла, число сообщений, фактическую частоту, сбои чтения и значения
вне диапазона отдельно; при наличии утилиты `mcap` — сводку `mcap info`. Журнал OpenOCD лежит рядом с
записью (`*.openocd.log`). Ctrl+C останавливает запись досрочно, записанное сохраняется.

## 3. Спецификация полей

Поля описываются в файле TOML (`--fields`, пример — `scripts/fields.toml` для прошивки-зонда
[stm32-skills-probe](https://github.com/ViacheslavMezentsev/stm32-skills-probe)). Каждое поле — таблица
`[[field]]`, одно 32-битное слово по адресу:

```toml
[[field]]
topic = "/probe/frames"           # имя канала в файле
message = "std_msgs/UInt32"       # тип сообщения
kind = "uint32"                   # как трактовать прочитанное слово
expr = "&probe_telemetry.frames"  # адрес: выражение C, его вычисляет GDB по ELF

[[field]]
topic = "/probe/tick_ms"          # счётчик платы: точное время без доверия к часам хоста
message = "std_msgs/UInt32"
kind = "uint32"
expr = "&platform_tick"

[[field]]
topic = "/probe/pin_frame"
message = "std_msgs/UInt32"
kind = "uint32"
expr = "svd:GPIOB.ODR.ODR15"      # поле регистра из SVD: сдвиг и маска применяются сами
```

Откуда берётся синтаксис каждого ключа:

| Ключ | Чем определяется | Примеры |
|---|---|---|
| файл | формат [TOML](https://toml.io/ru/); `[[field]]` — массив таблиц, читает `resolve_addresses.py` | |
| `topic` | любое имя; по обычаю ROS — `/группа/имя`, так каналы и называются в PlotJuggler | `/probe/frames` |
| `message` | имя типа ROS1 `std_msgs`; описания и контрольные суммы — в `msg_types.py`; тип должен вмещать значение после `kind` | `std_msgs/UInt32`, `Int32`, `Float32` |
| `kind` | список навыка: как трактовать 32-битное слово | `uint32`, `int32`, `float32` (биты IEEE-754), `uint32_seconds` (значение / 1000) |
| `expr` | выражение языка C в том виде, как его понимает GDB (`print &...`): GDB вычисляет его офлайн по отладочной информации DWARF в ELF, результат — адрес | `&var`, `&obj.field`, `&arr[3]`, `0x20000100` |
| `expr` с `svd:` | синтаксис навыка `svd:ПЕРИФЕРИЯ.РЕГИСТР[.ПОЛЕ]`; имена — из SVD-файла производителя (формат CMSIS-SVD), считает `svd_lookup.py` | `svd:GPIOB.ODR`, `svd:ADC1.SR.EOC` |
| `min`, `max` | числа в единицах после `kind`; значения вне диапазона отбрасываются и считаются отдельно | `min = -1000` |

Что не подходит для `expr`: локальные переменные функций и регистры ядра (их адрес существует только у
работающей цели), переменные, которые компилятор убрал или держит в регистре: GDB сообщит об ошибке.
Неизвестный `kind` отклоняется до подключения. Два поля с одним `expr` (одно значение в двух видах) остаются
двумя полями. Проверить выражение можно заранее:
`arm-none-eabi-gdb-py3 -batch -ex "print &probe_telemetry.frames" прошивка.elf`.

**Ловушка:** смещение на 4 байта даёт правдоподобное, но неверное значение (соседние `float` неотличимы по
виду). Сверяйте с эталоном, а не только с DWARF.

**Время.** Все поля одного цикла получают общую метку часов хоста. Для точных интервалов добавляйте в поля
счётчик платы (у зонда — `platform_tick`).

## 4. Без платы: Renode

Renode запускается отдельно с открытым монитором, прошивка работает (`start`), скрипт читает те же адреса
через монитор, не останавливая эмуляцию:

```sh
renode --disable-gui --port 1234 -e "include @run.resc"    # run.resc: mach create, LoadPlatformDescription, LoadELF, start
python scripts/record_diag.py --backend renode --record mcap --seconds 10
```

Время в Renode виртуальное и обычно идёт медленнее реального: метки хоста в файле его не отражают, для
интервалов нужен счётчик платы (`/probe/tick_ms`). Регистры, которых нет в модели платформы, читаются как 0,
счётчик тактов DWT в Renode не работает. Готовые задачи — в зонде
[stm32-skills-probe](https://github.com/ViacheslavMezentsev/stm32-skills-probe) (`.vscode/tasks.json`).

## 5. Другие MCU

Поддержка не-STM32 возможна и определяется не навыком, а цепочкой инструментов, поэтому подтверждается
опытным путём для каждой пары «пробник + MCU». Нужно, чтобы:

| Условие | Чем задаётся или проверяется |
|---|---|
| OpenOCD знает пробник | `--interface` (`interface/stlink.cfg`, `cmsis-dap.cfg`, `jlink.cfg`, `ftdi/...`); при нескольких — `--serial` |
| OpenOCD знает цель | `--target`: штатный скрипт (`target/nrf52.cfg`, `rp2040.cfg`, `gd32e23x.cfg`) или скрипт из OpenOCD производителя (у Artery — `target/at32f403axx.cfg` в их сборке, путь к ней — `--openocd`) |
| память читается без остановки ядра | так у большинства Cortex-M (доступ через MEM-AP); мешают защита от чтения (RDP, APPROTECT), сброс при подключении в скрипте цели (`--openocd-command "reset_config none"`) и выбор ядра у многоядерных |
| ELF с отладочной информацией | сборка с `-g`; GDB с Python для ARM (`arm-none-eabi-gdb-py3`, `gdb-multiarch`) |
| SVD (только для `svd:`) | файл производителя из его CMSIS-pack или SDK |
| Renode (без платы) | модель платформы MCU в Renode (`.repl`) |

Порядок проверки нового MCU:

1. `--doctor` с теми же ключами, что и у записи: `python scripts/record_diag.py --interface interface/cmsis-dap.cfg
   --target target/nrf52.cfg --elf app.elf --fields my_fields.toml --doctor`. Доктор проверяет конфигурации
   OpenOCD, ответ пробника, тип ядра и состояние цели (`running`), адреса полей по ELF и чтение одного слова
   без остановки ядра. Все строки «ок» — ключи совместимы.
2. `--dryrun` — посмотреть команду OpenOCD целиком.
3. Короткая запись (`--seconds 5`) и сверка с эталоном: счётчик, который должен расти, растёт; известная
   константа совпадает; фактическая частота близка к заданной.
4. Результат записать (MCU, пробник, версия OpenOCD, ключи) — в план исследований или в этот раздел.

## 6. Задача VS Code

```jsonc
// .vscode/tasks.json; путь к навыку — там, куда он установлен в проекте
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "Записать диагностику (mcap, 30 с)",
      "type": "shell",
      "command": "python",
      "args": [
        "${workspaceFolder}/.claude/skills/stm32-diag-record/scripts/record_diag.py",
        "--elf", "${workspaceFolder}/build/${input:preset}/<имя прошивки>.elf",
        "--record", "mcap", "--seconds", "30"
      ],
      "problemMatcher": []
    }
  ],
  "inputs": [
    { "id": "preset", "type": "pickString", "options": ["f411ce-debug", "f103c8-debug"], "description": "Пресет" }
  ]
}
```

Файл появится рядом с ELF (`build/<пресет>/diag/`), поэтому записи разных пресетов не смешиваются.

## 7. Форматы

| | mcap | bag |
|---|---|---|
| Открыть в PlotJuggler | загрузчиком MCAP, без ROS, в том числе под Windows (разбор схем `ros1msg` подтверждается в R9 плана) | только с плагином ROS1, то есть под Linux с ROS |
| Проверить | `mcap info`, Python-пакет `mcap` | `rosbag info` (ROS) или Python-пакет `rosbags` |
| Сжатие | нет | `--compress bz2` |
| Длинные записи | `--chunk-size` делит на чанки | сообщения копятся в памяти до конца записи |

## 8. Что не работает и почему

| Симптом | Причина и решение |
|---|---|
| «не удалось вычислить адреса» | нет GDB с Python (`GDB=<путь>`), неверный `--elf` или поле `svd:` без `--svd` |
| «OpenOCD не поднялся» | пробник занят другим OpenOCD или порт занят: журнал `*.openocd.log`, `--use-running` или `--port` |
| в `--doctor` «Пробник НЕТ» | не подключён, нет драйвера или не тот `--interface`; при нескольких пробниках — `--serial` |
| в `--doctor` «Конфигурации OpenOCD НЕТ» | скрипта цели нет в этой сборке OpenOCD: свой `.cfg` по пути или OpenOCD производителя (`--openocd`) |
| в `--doctor` «Цель НЕТ» при найденном пробнике | нет питания или SWD, защита от чтения, неверный скрипт цели, низкая скорость (`--openocd-command "adapter speed 1000"`) |
| значения замерли | прошивка остановлена (точка останова, `reset halt`) или адрес указывает на регистр, который не меняется |
| фактическая частота ниже заданной | много далёких друг от друга адресов: каждая группа — отдельная команда; сократите поля или `--rate` |
| прошивка перезапустилась при старте записи | использован `--reset` |
| «монитор Renode не ответил» | Renode запущен без `--port` или на другом порту |
| «не выбрана машина» | в мониторе нет текущей машины: `--machine` с именем из `mach create` |
| значения из Renode растут медленно | время эмуляции виртуальное: см. раздел 4 |

## 9. Как проверяется в CI

Задание Emulation репозитория: `resolve_addresses.py` разрешает поля примера на ELF зонда (F411CE, SVD
STM32F411), `record_diag.py --use-running` пишет mcap и bag через поддельный telnet OpenOCD
(`ci/fake_openocd.py`), а файлы читаются независимыми библиотеками `mcap` и `rosbags`; `--backend renode`
пишет mcap из настоящего Renode 1.17 с работающим зондом (счётчики растут, поле `svd:` читается). Чтение по
SWD настоящим OpenOCD проверяется на стенде.

## Файлы навыка

```
scripts/record_diag.py       вход: доктор, dryrun, запись mcap/bag (OpenOCD или Renode)
scripts/fields.toml          пример спецификации полей (прошивка-зонд)
scripts/resolve_addresses.py адреса по ELF (GDB) и SVD, офлайн        ┐ общие модули: оригинал —
scripts/svd_lookup.py        имена регистров SVD -> адреса             │ stm32-ros-alt-channel/scripts,
scripts/bag_writer.py        писатель rosbag v2.0 (stdlib, bz2)        │ копии обновляет
scripts/mcap_writer.py       писатель MCAP (stdlib, чанки)             │ ci/sync_shared.py
scripts/msg_types.py         типы std_msgs, md5 сверены с ROS          ┘
```

Пути считаются от каталога `scripts/`; ELF, SVD и файлы записи задаются аргументами или переменными
окружения.
