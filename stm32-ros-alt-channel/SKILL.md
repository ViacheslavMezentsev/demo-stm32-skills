---
name: stm32-ros-alt-channel
description: 'Вывод данных прошивки STM32 в ROS1 без её изменения и без занятия UART: нода читает переменные и регистры из RAM по SWD через OpenOCD (команда mdw) либо берёт штатные топики rosserial по UART, и публикует их в ROS-графе внутри WSL; запись в rosbag или MCAP своим писателем на стандартной библиотеке, адреса считаются автоматически по ELF и SVD. Применяй, когда нужен диагностический канал: свои поля, регистры периферии, запись файлов без установки ROS под Windows, просмотр через rostopic и PlotJuggler.'
license: MIT
compatibility: Windows с Python 3.8+, OpenOCD и ST-Link; WSL2 (Ubuntu) с ROS Noetic и socat; WSL в режиме mirrored networking, чтобы обращаться к службам Windows по 127.0.0.1; счётчик времени ядра WSL должен быть исправен (см. раздел про время)
metadata:
  author: Viacheslav Mezentsev
  version: "1.1.0"
  kind: diagnostic-channel
  transports: swd-openocd, uart-rosserial
  formats: rosbag2-ros1, mcap
  tools: python3, openocd, socat, rosrun, mcap
allowed-tools: Bash Read Write Edit Glob Grep
---

# Канал «прошивка STM32 → ROS1»: два маршрута, запись bag/MCAP

Применяй, когда нужны данные платы **в ROS без правки прошивки**: посмотреть значения в `rostopic`, снять
файл для PlotJuggler, вытащить регистр периферии. Всё, что нужно, — скрипты из комплекта навыка
(`scripts/`) плюс инструменты, перечисленные в `compatibility`.

## 1. Два маршрута

| | A: OpenOCD (чтение памяти) | B: rosserial (UART) |
|---|---|---|
| Источник | RAM через SWD, ядро не останавливается | UART прошивки |
| Что видно | поля, описанные в спецификации | все топики прошивки |
| Root/socat | **не нужны** | нужны (в WSL берутся через `-u root`) |
| UART | свободен | занят |
| Двусторонность | нет | да |
| Когда выбирать | свои величины, регистры, прошивка молчит | штатные топики |

Оба маршрута заканчиваются в одном ROS-графе **внутри WSL**: под Windows ROS1 официально не
поддерживается, а WSL видит службы Windows по `127.0.0.1` (mirrored networking) — поэтому отдельный мост
для маршрута A не нужен.

## 2. Команды

```sh
python scripts/ros1_channel.py --doctor                 # проверка окружения и остатков прошлого запуска
python scripts/ros1_channel.py --dryrun                 # проверки + план, ничего не запускает
python scripts/ros1_channel.py --via openocd --keep     # маршрут A, смотреть вживую
python scripts/ros1_channel.py --via rosserial --keep   # маршрут B, смотреть вживую
python scripts/ros1_channel.py --via both --keep        # оба сразу (SWD в отдельном дереве /swd/...)
python scripts/ros1_channel.py --via openocd --record mcap --seconds 10 --out probe.mcap
python scripts/ros1_channel.py --via rosserial --record bag --compress bz2 --seconds 20
python scripts/ros1_channel.py --cleanup                # остановить всё и проверить остатки
```

Параметры: `--via openocd|rosserial|both`, `--record none|bag|mcap`, `--out ИМЯ`,
`--topics all|a,b,c`, `--seconds N`, `--compress none|bz2`, `--chunk-size N`, `--keep`, `--cleanup`,
`--elf ПУТЬ`, `--profile F411|G474`, `--port COM7`, `--baud 460800`, `--tcp 7777`, `--pty /dev/ttyVirtual0`.

При `--via both` маршрут A публикуется в дерево `/swd/...`, чтобы имена не совпали с rosserial-топиками.

## 3. Зависимости на стороне MCU (маршрут A)

| Условие | Почему |
|---|---|
| Переменная **существует в RAM** (`.bss`/`.data`) | значение в регистре прочитать нельзя |
| Поле ≤ 4 байт, адрес кратен 4 | читается 32-битное слово |
| **Прошивка исполняется** | после `reset halt` значения замирают |
| ELF совпадает с прошитой сборкой | адреса берутся из него |
| Нет кэша данных (M4) | на M7 нужна отдельная очистка |

**Не требуется:** RTT, `ros_lib`, публикация, свободный UART, правка кода.

## 4. Как задать свои значения

Поля описываются в спецификации (`fields.toml`), три способа:

```toml
expr = "&some_counter"                                    # переменная прошивки
expr = "(char*)&some_struct + (long)&((Type*)0)->field"   # поле структуры (смещение по DWARF)
expr = "svd:ADC1.DR"                                      # регистр периферии по имени из SVD
expr = "svd:ADC1.SR.EOC"                                  # поле регистра: сдвиг и маска автоматически
```

Адреса пересчитываются при запуске офлайн по ELF (плата не нужна). Проверить имя из SVD отдельно:

```sh
python scripts/svd_lookup.py ADC1.SR.EOC   # адрес, сдвиг, маска
```

**Ловушка:** смещение на 4 байта даёт правдоподобное, но неверное число (соседние `float` неотличимы по
виду). Сверяй с эталонным прибором, а не только с DWARF.

## 5. Запись и переиндексация

Запись делает `scripts/recorder_node.py` внутри WSL через `rospy.AnyMsg`: берёт уже сериализованные байты
и `connection_header` (тип, md5, определение), поэтому пишутся **любые** топики, включая собственные типы.

| Ключ | Значение | Смысл |
|---|---|---|
| `--compress` | `none`, `bz2` | сжатие чанка bag (`bz2` — stdlib, читается `rosbag` везде) |
| `--chunk-size` | число сообщений | несколько чанков в MCAP (`zstd`/`lz4` не используются: внешние пакеты) |

Если bag остался без индекса (запись прервана), канал копирует оригинал в `<имя>.bag.bak`, запускает
`rosbag reindex` и убирает служебный `.orig.bag`. MCAP переиндексация не нужна.

## 6. Точность времени: проверять перед измерениями

`monotonic` в WSL **не годится** для поиска пауз: он замирает вместе с гостем. Нужна внешняя опора.

| Симптом | Причина | Решение |
|---|---|---|
| паузы 1.5–2.8 с каждые ~30 с у всех топиков | неверный `tick` ядра WSL | привести `tick` к `10000`, отключить `systemd-timesyncd`, `wsl --shutdown` |
| данные «замерли», плата работала | подвод часов гостя | тот же |
| нужно мерить время без доверия к ВМ | — | брать счётчик платы (`uwTick`, 1 мс) в записи |

Проверка тика: `wsl -d <дистрибутив> -u root -e python3 scripts/wsl_tick.py` → ожидается `tick = 10000`.
Опора на часы хоста: `scripts/host_clock.py` на Windows и `scripts/host_ref_probe.py` в WSL; норма —
`mono/host ≈ 1.000` и «шагов 0».

## 7. Что не работает и почему

| Симптом | Причина |
|---|---|
| «мастер не поднялся» при свободном порте | устаревший `~/.ros/roscore-*.pid` → `--cleanup` |
| `serial_node`: `ConnectionRefusedError` | мастер убит общей уборкой → `--cleanup`, запустить заново |
| порт читается пустым | rosserial начинает передачу только после согласования с `serial_node` |
| `rostopic list` пуст | `serial_node` не успел согласовать, подожди 5–10 с |
| ROS «остался работать» после остановки | `terminate()` клиента `wsl.exe` не убивает процесс в WSL → `--cleanup` делает `pkill -9` с проверкой |
| «Unexpected command line argument» у OpenOCD | аргументы собраны через `split(" ")` → передавай список |

## Файлы навыка

```
scripts/ros1_channel.py      вход: маршруты, доктор, dryrun, запись, переиндексация
scripts/recorder_node.py     запись любых топиков через AnyMsg
scripts/telemetry_node.py    нода маршрута A (чтение памяти по адресам)
scripts/resolve_addresses.py расчёт адресов по ELF и SVD
scripts/fields.toml          спецификация полей
scripts/svd_lookup.py        имена регистров SVD → адреса
scripts/bag_writer.py        писатель rosbag v2.0 (stdlib)
scripts/mcap_writer.py       писатель MCAP (stdlib)
scripts/msg_types.py         реестр стандартных типов, md5 сверены с ROS
scripts/host_clock.py        часы хоста для проверки времени
scripts/host_ref_probe.py    опора в каждом такте (WSL)
scripts/wsl_tick.py          проверка tick ядра
```
