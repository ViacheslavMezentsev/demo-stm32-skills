---
name: stm32-renode-emulator
description: 'Песочница Renode 1.17 и QEMU для прошивки STM32 без платы: прогон стартового лога (G474 в Renode на макете с заглушками периферии, F411 в QEMU), проверка лога по ожиданиям и данным ELF, совместимость макета с Renode 1.16.1, отладка через GDB-сервер Renode. Использовать для быстрой проверки гипотез до аппаратного опыта, в CI и как источник эмуляционных артефактов.'
license: MIT
compatibility: Renode 1.17 (1.16.1 — только совместимость), QEMU для профиля F411, Python 3.8+ (проверка лога — 3.11+ или пакет tomli), arm-none-eabi-objcopy; стенд не требуется
metadata:
  author: Viacheslav Mezentsev
  version: "1.1.0"
  workflow: исследование → навык → проект
allowed-tools: Bash Read Write Edit Glob Grep
---

# Renode: песочница без платы

Применяй, когда нужно: проверить гипотезу **до** аппаратного опыта (сборка, старт, ранняя
инициализация, порядок вывода), получить артефакты эмуляции для отчёта, убедиться, что прошивка вообще
доходит до нужного этапа, не занимая стенд.

**Что это даёт и чего не даёт.** Эмулятор воспроизводит **лог загрузки** и раннюю инициализацию.
Он **не** даёт аппаратной приёмки: модель периферии — заглушки, время не соответствует реальному,
прерывания и DMA ведут себя иначе. Вывод из эмуляции помечается `emulator`, аппаратные выводы из него
не следуют.

## 1. Маршруты

| Профиль | Эмулятор | Машина | Вывод прошивки |
| --- | --- | --- | --- |
| `G474` | Renode 1.17 | макет `assets/stm32g4_mock.repl`: Cortex-M4, Flash 512 КБ, SRAM 128 КБ, заглушки периферии | семихостинг → `cpu.semihosting.uart` → файл |
| `F411` | QEMU (`qemu-system-arm`) | `netduinoplus2` (STM32F405, тот же Cortex-M4) | семихостинг → chardev-файл |

Прошивка печатает стартовый лог через семихостинг (`SYS_WRITE0`), строки с префиксом времени
`[   0.001] `. В QEMU прошивка должна завершиться (`SYS_EXIT`), иначе прогон закончится по таймауту;
Renode останавливается сам после одной виртуальной секунды.

## 2. Прогон

```sh
python scripts/run.py --profile G474 --elf build/firmware.elf
python scripts/run.py --profile G474 --elf build/firmware.elf --verify --expected boot.toml
python scripts/run.py --profile F411 --elf build/firmware.elf --qemu qemu-system-arm
```

- Renode ищется так: `--renode` или `RENODE_BINARY`, затем `/opt/renode-1.17.0/renode`, затем `PATH`
  (на Windows — ещё `Program Files\Renode`). Версия обязана определиться: 1.17 — основная,
  1.16.1 — с предупреждением, остальные отклоняются.
- Свой макет вместо штатного — `--platform <файл.repl>`.
- Каждая попытка пишет в **новый** каталог (`--output` или `<каталог ELF>/emulation/<время>-<id>`):
  `startup.log`, `emulator.log`, `run.json` (статус, версия эмулятора, SHA-256 ELF, команда).
- Код возврата: 0 — PASS или CAPTURED, 1 — FAIL проверки, 2 — ERROR (нет эмулятора, лога, таймаут).

Ручной прогон и отладка без `run.py` — сценарии из `assets/`:

```sh
renode --disable-gui --console --plain \
  -e '$platform=@assets/stm32g4_mock.repl; $log=@build/startup.log; $bin=@build/firmware.elf; include @assets/boot.resc'
renode -e '$platform=@assets/stm32g4_mock.repl; $log=@build/startup.log; $bin=@build/firmware.elf; include @assets/debug.resc'
arm-none-eabi-gdb build/firmware.elf -ex "target remote :3334"
```

`debug.resc` поднимает GDB-сервер на порту 3334 и не стартует ядро (как `-S` у QEMU).

## 3. Проверка стартового лога

`--verify` запускает `scripts/check_startup_log.py`. Ожидания — TOML-файл прошивки (образец —
`assets/expected.example.toml`):

| Ключ | Смысл |
| --- | --- |
| `min_lines` | минимум строк — защита от оборванного лога |
| `first_line`, `last_line` | регулярные выражения для первой и последней строки |
| `[require].lines` | выражения, каждое обязано найтись |
| `[forbid].lines` | выражения, которых быть не должно (признаки чужой среды, отказа) |

Подстановки `{version}`, `{build}`, `{revision}` берутся из секции `.fw_version` ELF
(`u8 major, minor, patch, reserved; u16 build, revision`), `{crc32}` — из `.checksum`, `{size}` — размер
образа. Так проверяется, что эмулятор напечатал данные **именно этой** сборки. Строки без префикса
времени считаются ошибкой: лог должен идти от `sys_log` прошивки. Отчёт — `startup-log-report.json`.

## 4. Версии Renode и макет

- В 1.17 семихостинг подключается отдельным обработчиком `CPU.SemihostingHandler`, путь UART —
  `cpu.semihosting.uart`. В 1.16.1 UART висит прямо на процессоре (`sysbus.cpu.uart`). Один макет обе
  версии не принимают; `scripts/renode_compat.py` готовит копию макета под найденную версию.
- Тип ядра — `cortex-m4`, а не `cortex-m4f`: в 1.16.1 `cortex-m4f` отдаёт CPUID Cortex-M7. CPUID
  `cortex-m4` — `0x410FC241` в 1.16.1 и `0x410FC240` в 1.17.0, поэтому проверять стоит номер ядра
  (`0xC24`), а не ревизию.
- Заглушки периферии — `Memory.MappedMemory`: запись сохраняется, чтение возвращает записанное, флаги
  готовности сами не выставляются. Код, ждущий бит готовности (PLL, HSE, АЦП), в эмуляторе зависнет —
  такие ожидания в прошивке обходят проверкой среды. Адреса заглушек сверены с `STM32G474.svd`
  (это же проверяет CI репозитория).

## 5. Как это проверяется в CI репозитория

`ci/run_checks.py emulation` собирает прошивку-зонд
[stm32-skills-probe](https://github.com/ViacheslavMezentsev/stm32-skills-probe) для F411CE, F401CC и F103C8
с семихостингом и выходом по времени (`-DPROBE_LOG_SEMIHOSTING=ON -DPROBE_EXIT_MS=500`), прогоняет её через
`run.py` в Renode 1.17 (модель зонда, `--platform`) и в QEMU с `--verify` по ожиданиям зонда
(`emu/expected/<плата>.toml`), убеждается, что оборванный лог отклоняется, и сверяет адреса макета G474 и
модели зонда с SVD через `svd_lookup.py` навыка stm32-ros-alt-channel. Это доказательство работы комплекта
в эмуляторе, а не на плате.

## Файлы навыка

```
assets/
  stm32g4_mock.repl      макет G474 для Renode 1.17: ядро, память, заглушки периферии
  boot.resc              ручной прогон: стартовый лог в файл, одна виртуальная секунда
  debug.resc             отладка: GDB-сервер на 3334, ядро ждёт подключения
  expected.example.toml  образец ожиданий к стартовому логу
scripts/
  run.py                 прогон в Renode (G474) или QEMU (F411), проверка лога (--verify)
  check_startup_log.py   проверка лога по ожиданиям и данным ELF
  renode_compat.py       различия Renode 1.16.1 и 1.17: форма макета, путь UART, версия
```

Пути — относительно каталога навыка; ELF, ожидания и каталоги прогонов задаются аргументами.
