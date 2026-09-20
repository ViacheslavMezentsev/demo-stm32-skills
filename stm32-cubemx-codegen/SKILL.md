---
name: stm32-cubemx-codegen
description: >
  Автоматизирует запуск STM32CubeMX в режиме командной строки для генерации
  файлов проекта из существующего .ioc файла. Применяется когда нужно
  сгенерировать или регенерировать дерево файлов проекта STM32 без открытия
  GUI CubeMX. Активировать при словах "сгенерировать проект", "запустить CubeMX",
  "regenerate", "generate code", "ioc файл".
license: MIT
compatibility: >
  Требует установленного STM32CubeMX (6.x+) и Java (входит в состав CubeMX).
  Windows: java.exe в составе CubeMX. Linux/macOS: STM32CubeMX в PATH.
metadata:
  version: "0.1"
  manual: UM1718
---

# stm32cubemx-codegen

## Принцип работы

STM32CubeMX поддерживает режим командной строки (`-q`): принимает скрипт
с командами, выполняет их без GUI и завершается. Это позволяет агенту
сформировать скрипт, запустить CubeMX и получить готовое дерево файлов проекта.

Полный справочник команд — в [references/COMMANDS.md](references/COMMANDS.md).

---

## Шаг 1. Определить окружение

Агент должен выяснить у пользователя (или определить самостоятельно):

| Параметр | Пример (Windows) | Пример (Linux) |
|---|---|---|
| Путь к CubeMX | `C:\Users\User\AppData\Local\Programs\STM32CubeMX\STM32CubeMX.exe` | `/opt/STM32CubeMX/STM32CubeMX` |
| Путь к `.ioc` | `D:\projects\demo-stm32-cmake\stm32f4xx\07-pwm\07-pwm.ioc` | `/home/user/demo-stm32-cmake/stm32f4xx/07-pwm/07-pwm.ioc` |
| Toolchain | `Makefile` (по умолчанию) | |
| Папка вывода | папка с `.ioc` файлом (по умолчанию) | |

---

## Шаг 2. Сформировать скрипт `.mxscript`

Создать текстовый файл со списком команд — по одной на строку.

### Минимальный скрипт (регенерация из существующего .ioc):

```
config load "<path_to_ioc>"
project toolchain Makefile
SetCopyLibrary "copy as reference"
project generate
exit
```

### Скрипт с явным указанием параметров:

```
config load "<path_to_ioc>"
project name <project_name>
project path "<output_path>"
project toolchain Makefile
project couplefilesbyip 1
SetCopyLibrary "copy as reference"
project generate
exit
```

### Скрипт для создания проекта с нуля (без .ioc):

```
load <MCU_NAME>
project name <project_name>
project path "<output_path>"
project toolchain Makefile
project couplefilesbyip 1
SetCopyLibrary "copy as reference"
config save "<path_to_ioc>"
project generate
exit
```

**Правила формирования скрипта:**
- Пути с пробелами — в двойных кавычках.
- `SetCopyLibrary "copy as reference"` — всегда использовать этот режим.
  Драйверы не копируются в папку проекта; `stm32-cmake-yml` найдёт их автоматически.
- `project couplefilesbyip 1` — генерировать инициализацию периферии
  в отдельных `.c`/`.h` файлах (рекомендуется).
- `project generate` требует авторизации для скачивания пакетов.
  Если пакет уже установлен локально — авторизация не нужна.

---

## Шаг 3. Запустить CubeMX

### Windows

```powershell
$cubemx = "C:\Users\User\AppData\Local\Programs\STM32CubeMX\STM32CubeMX.exe"
$script = "C:\path\to\generate.mxscript"

& "$cubemx\..\..\jre\bin\java.exe" -jar "$cubemx" -q $script
```

Или через `cmd`:

```bat
cd "C:\Users\User\AppData\Local\Programs\STM32CubeMX"
jre\bin\java.exe -jar STM32CubeMX.exe -q "C:\path\to\generate.mxscript"
```

### Linux / macOS

```bash
/opt/STM32CubeMX/STM32CubeMX -q /path/to/generate.mxscript
```

### Флаги запуска

| Флаг | Режим |
|---|---|
| `-i` | Интерактивный (показывает prompt `MX>`) |
| `-s <file>` | Скрипт с UI |
| `-q <file>` | Скрипт **без UI** (рекомендуется для автоматизации) |

---

## Шаг 4. Проверить результат

После успешного выполнения CubeMX создаст в папке проекта:

```
<output_path>/
├── Core/
│   ├── Inc/
│   │   ├── main.h
│   │   ├── stm32<family>xx_hal_conf.h
│   │   └── stm32<family>xx_it.h
│   └── Src/
│       ├── main.c
│       ├── stm32<family>xx_hal_msp.c
│       ├── stm32<family>xx_it.c
│       ├── system_stm32<family>xx.c
│       └── <peripheral>.c   # при couplefilesbyip 1
├── Makefile
└── <project>.ioc
```

Агент должен убедиться, что файлы созданы, после чего можно переходить
к навыку `demo-stm32-cmake-example-builder`.

---

## Типовой сценарий: создать пример с нуля

1. Пользователь сообщает: MCU, имя проекта, папку, нужную периферию.
2. Агент формирует `.mxscript` с командами `load`, настройками проекта и `project generate`.
3. Агент запускает CubeMX с флагом `-q`.
4. Агент проверяет наличие `Core/Src/main.c` в папке проекта.
5. Агент применяет навык `demo-stm32-cmake-example-builder` для добавления
   `CMakeLists.txt`, `stm32_config.yml`, `.vscode/` и `program.cpp`.

## Типовой сценарий: регенерация после правки .ioc

1. Пользователь изменил `.ioc` вручную или сообщил о необходимости регенерации.
2. Агент формирует минимальный скрипт с `config load` + `project generate`.
3. Запускает CubeMX `-q`.
4. Проверяет, что `main.c` обновился (дата изменения).

---

## Важные ограничения

- **CubeMX не конфигурирует периферию через CLI.** Назначение пинов, настройка
  тактирования, выбор IP-блоков — только через GUI или через готовый `.ioc` файл.
  CLI умеет только загрузить конфигурацию и сгенерировать код.
- Команда `project generate` может потребовать `login` при первом запуске
  или при отсутствии пакета локально.
- На Linux/macOS путь к CubeMX может отличаться. Уточнить у пользователя
  или проверить стандартные места установки.
