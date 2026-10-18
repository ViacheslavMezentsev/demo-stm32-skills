---
name: stm32-diag-record
description: 'Запись диагностических полей прошивки STM32 в файл mcap или bag без ROS: один скрипт поднимает OpenOCD (без сброса цели), читает переменные, поля структур и регистры периферии по SWD, пока прошивка работает, и сохраняет файл заданной длительности; адреса считаются офлайн по ELF и SVD. Использовать, когда нужно снять значения с платы для офлайн-разбора в PlotJuggler или mcap/rosbag, в том числе из задачи VS Code, без установки ROS и WSL.'
license: MIT
compatibility: Windows или Linux с Python 3.9+ (3.11+ или пакет tomli для resolve_addresses.py), OpenOCD 0.11+ и SWD-пробник (ST-Link и др.), GDB с Python для адресов по ELF, SVD-файл для полей svd:; ROS не требуется
metadata:
  author: Viacheslav Mezentsev
  version: "1.1.0"
  kind: diagnostic-recorder
  outputs: mcap, rosbag-ros1
  entrypoint: scripts/record_diag.py
  analog: stm32-ros-alt-channel
allowed-tools: Bash Read Write Edit Glob Grep
---

# Запись диагностики с платы без ROS

Один скрипт: поднимает OpenOCD, читает выбранные поля прошивки по SWD и пишет файл `mcap` или `bag`.
ROS не нужен ни для записи, ни для чтения. Если нужны живые топики в ROS-графе, — навык
`stm32-ros-alt-channel`; общие модули у навыков одинаковые (см. «Файлы навыка»).

## 1. Что происходит

```
ELF (+SVD) -> resolve_addresses.py -> адреса полей            (офлайн, плата не нужна)
плата (RAM) -> SWD -> OpenOCD (telnet) -> mdw по адресам -> сериализация std_msgs -> mcap | bag
```

OpenOCD подключается **без сброса**: прошивка продолжает работать с того места, где была (`--reset` —
только если сброс нужен). Ядро не останавливается, UART не занимается. Соседние адреса (в окне 256 байт)
читаются одной командой `mdw`, невыровненное поле собирается из двух слов.

## 2. Команды

```sh
python scripts/record_diag.py --doctor                  # проверка окружения и пробников
python scripts/record_diag.py --dryrun --seconds 10     # план и команда OpenOCD, ничего не запускается
python scripts/record_diag.py --record mcap --seconds 30
python scripts/record_diag.py --record bag --compress bz2 --seconds 30 --out diag.bag
python scripts/record_diag.py --target target/stm32f1x.cfg --serial <серийный номер> --record mcap
python scripts/record_diag.py --use-running --port 4444 --record mcap   # OpenOCD уже запущен
```

| Ключ | Смысл |
|---|---|
| `--record mcap\|bag` (он же `--format`) | формат файла; по умолчанию `mcap` |
| `--out ПУТЬ` | файл записи; по умолчанию `<каталог ELF>/diag/record-<дата>.<формат>` |
| `--seconds N`, `--rate ГЦ` | длительность (10 с) и частота опроса (10 Гц) |
| `--fields ПУТЬ` | спецификация полей (TOML), по умолчанию `scripts/fields.toml` |
| `--elf ПУТЬ` | ELF; иначе `$FIRMWARE_ELF`, иначе первый `build/*/*.elf` |
| `--svd ПУТЬ` | SVD для полей `svd:...`; иначе `$STM32_SVD` |
| `--interface`, `--target` | конфигурации OpenOCD: `interface/stlink.cfg`, `target/stm32f4x.cfg` (F1 — `target/stm32f1x.cfg`) |
| `--serial SN` | серийный номер пробника; иначе `$STLINK_SERIAL`, иначе первый найденный |
| `--reset` | сбросить цель после подключения |
| `--compress none\|bz2` | сжатие чанка bag |
| `--chunk-size N` | сообщений на чанк mcap |
| `--use-running`, `--host`, `--port` | подключиться к уже запущенному OpenOCD |
| `--addresses ПУТЬ` | куда сохранить JSON адресов (по умолчанию рядом с записью) |

После записи скрипт печатает размер файла, число сообщений, фактическую частоту, сбои чтения и значения
вне диапазона отдельно; при наличии утилиты `mcap` — сводку `mcap info`. Журнал OpenOCD лежит рядом с
записью (`*.openocd.log`). Ctrl+C останавливает запись досрочно, записанное сохраняется.

## 3. Спецификация полей

```toml
[[field]]
topic = "/probe/frames"           # имя в файле
message = "std_msgs/UInt32"       # тип сообщения
kind = "uint32"                   # как трактовать слово
expr = "(char*)&probe_telemetry + (long)&((ProbeTelemetry*)0)->frames"

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

`kind`: `uint32`, `int32`, `float32` (биты IEEE-754), `uint32_seconds` (значение / 1000). Неизвестный
`kind` отклоняется до подключения. `min`/`max` (необязательно) отбрасывают выбросы. Два поля с одним `expr`
(одно значение в двух видах) остаются двумя полями. Пример `scripts/fields.toml` — для прошивки-зонда
[stm32-skills-probe](https://github.com/ViacheslavMezentsev/stm32-skills-probe).

**Ловушка:** смещение на 4 байта даёт правдоподобное, но неверное значение (соседние `float` неотличимы по
виду). Сверяйте с эталоном, а не только с DWARF.

**Время.** Все поля одного цикла получают общую метку часов хоста. Для точных интервалов добавляйте в поля
счётчик платы (у зонда — `platform_tick`).

## 4. Задача VS Code

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

## 5. Форматы

| | mcap | bag |
|---|---|---|
| Открыть в PlotJuggler | напрямую | через плагин ROS1 |
| Проверить | `mcap info`, Python-пакет `mcap` | `rosbag info` (ROS) или Python-пакет `rosbags` |
| Сжатие | нет | `--compress bz2` |
| Длинные записи | `--chunk-size` делит на чанки | сообщения копятся в памяти до конца записи |

## 6. Что не работает и почему

| Симптом | Причина и решение |
|---|---|
| «не удалось вычислить адреса» | нет GDB с Python (`GDB=<путь>`), неверный `--elf` или поле `svd:` без `--svd` |
| «OpenOCD не поднялся» | пробник занят другим OpenOCD или порт занят: журнал `*.openocd.log`, `--use-running` или `--port` |
| «пробник не найден» в `--doctor` | не подключён или нет драйвера; при нескольких пробниках — `--serial` |
| значения замерли | прошивка остановлена (точка останова, `reset halt`) или адрес указывает на регистр, который не меняется |
| фактическая частота ниже заданной | много далёких друг от друга адресов: каждая группа — отдельная команда; сократите поля или `--rate` |
| прошивка перезапустилась при старте записи | использован `--reset` |

## 7. Как проверяется в CI

Задание Emulation репозитория: `resolve_addresses.py` разрешает поля примера на ELF зонда (F411CE, SVD
STM32F411), `record_diag.py --use-running` пишет mcap и bag через поддельный telnet OpenOCD
(`ci/fake_openocd.py`), а файлы читаются независимыми библиотеками `mcap` и `rosbags`. Это проверка
цепочки без платы; чтение по SWD настоящим OpenOCD проверяется на стенде.

## Файлы навыка

```
scripts/record_diag.py       вход: доктор, dryrun, запись mcap/bag
scripts/fields.toml          пример спецификации полей (прошивка-зонд)
scripts/resolve_addresses.py адреса по ELF (GDB) и SVD, офлайн        ┐ общие модули: оригинал —
scripts/svd_lookup.py        имена регистров SVD -> адреса             │ stm32-ros-alt-channel/scripts,
scripts/bag_writer.py        писатель rosbag v2.0 (stdlib, bz2)        │ копии обновляет
scripts/mcap_writer.py       писатель MCAP (stdlib, чанки)             │ ci/sync_shared.py
scripts/msg_types.py         типы std_msgs, md5 сверены с ROS          ┘
```

Пути считаются от каталога `scripts/`; ELF, SVD и файлы записи задаются аргументами или переменными
окружения.
