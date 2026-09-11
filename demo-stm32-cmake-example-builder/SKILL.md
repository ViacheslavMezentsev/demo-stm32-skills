---
name: demo-stm32-cmake-example-builder
description: >
  Создаёт нумерованные демо-примеры для репозитория demo-stm32-cmake
  (github.com/ViacheslavMezentsev/demo-stm32-cmake). Применяется после того,
  как пользователь сгенерировал базовый код в STM32CubeMX. Формирует все файлы
  проекта: CMakeLists.txt, stm32_config.yml, .vscode, линкер-скрипт .ld.in,
  program.cpp и вносит правки в main.h/main.c. Активировать при словах
  "новый пример", "создай проект STM32", "demo-stm32-cmake", "07-pwm" и подобных.
license: MIT
compatibility: >
  Репозиторий demo-stm32-cmake с подмодулями stm32-cmake и stm32-cmake-yml.
  Требует STM32CubeMX для генерации начального кода.
metadata:
  version: "0.3"
  repository: https://github.com/ViacheslavMezentsev/demo-stm32-skills
---

# demo-stm32-cmake-example-builder

## Зависимые навыки

Прочитай перед началом работы:

| Навык | Применять при |
|---|---|
| [`stm32-config-manager`](https://github.com/ViacheslavMezentsev/demo-stm32-skills/blob/main/stm32-config-manager/SKILL.md) | Формировании `stm32_config.yml` |
| [`cpp-clang-format`](https://github.com/ViacheslavMezentsev/demo-stm32-skills/blob/main/cpp-clang-format/SKILL.md) | Написании любого C/C++ кода |
| [`stm32-simple-sources`](https://github.com/ViacheslavMezentsev/demo-stm32-skills/blob/main/stm32-simple-sources/SKILL.md) | Формировании `sources` и `CMakeLists.txt` |

Если навык выполняется в составе репозитория, навыки доступны локально:
`demo-stm32-cmake/modules/demo-stm32-skills/<name>/SKILL.md`

---

## Структура проекта

```
demo-stm32-cmake/
├── modules/
│   ├── stm32-cmake/
│   └── stm32-cmake-yml/
└── stm32f4xx/
    └── 07-pwm/
        ├── .vscode/
        ├── Core/              # генерируется CubeMX
        ├── User/
        │   ├── CMakeLists.txt
        │   └── Src/program.cpp
        ├── <n>.ioc
        ├── stm32_config.yml
        ├── CMakeLists.txt
        ├── STM32F411XX_FLASH.ld.in
        └── STM32F411.svd
```

---

## Шаг 1. Подготовка в CubeMX (пользователь)

1. Создать папку `stm32<family>xx/<NN>-<name>/` в репозитории.
2. Открыть CubeMX, выбрать MCU, настроить нужную периферию.
3. В **Project Manager**:
   - `Toolchain / IDE` → **Makefile**
   - `Project Location` → папка примера
   - Режим копирования → **Add necessary files as reference**
     *(драйверы не копируются, `stm32-cmake-yml` найдёт их автоматически)*
4. **Generate Code**.
5. Сообщить AI: папку, MCU, тему; приложить `.ioc`, `main.c`, `main.h`.

---

## Шаг 2. Анализ сгенерированного кода

Из `.ioc` извлечь:
- MCU — `ProjectManager.DeviceId` (убрать trailing `x`)
- Имя проекта — `ProjectManager.ProjectName`
- Версия FW — `ProjectManager.FirmwarePackage`
- Heap/Stack — `ProjectManager.HeapSize` / `StackSize` (из HEX в dec)
- Активная периферия — `Mcu.IP*` → для `hal_components`
- Тактирование — источник, SYSCLK, APB1/APB2

> `stm32-cmake-yml` читает `.ioc` сам через параметр `ioc_file`.
> Дублировать MCU/heap/stack в YAML не нужно, если они совпадают с `.ioc`.

Из `main.c` — список `MX_*_Init()` для `Core/CMakeLists.txt`.
Из `main.h` — макросы пинов для использования в `program.cpp`.

---

## Шаг 3. Создаваемые файлы

### 3.1. `CMakeLists.txt` (корень)

Идентичен для всех примеров. Не изменять.

```cmake
cmake_minimum_required(VERSION 3.19)

set(CMAKE_TOOLCHAIN_FILE "${CMAKE_CURRENT_SOURCE_DIR}/../../modules/stm32-cmake/cmake/stm32_gcc.cmake")
set(STM32_YML_FRAMEWORK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../modules/stm32-cmake-yml")
list(APPEND CMAKE_MODULE_PATH "${STM32_YML_FRAMEWORK_DIR}")
include(stm32_yml)

stm32_yml_prepare_project_data(PROJECT_NAME PROJECT_LANGUAGES)
project(${PROJECT_NAME} LANGUAGES ${PROJECT_LANGUAGES})
stm32_yml_setup_project(${PROJECT_NAME})
```

### 3.2. `Core/CMakeLists.txt`

Все `.c` из `Core/Src/`, **кроме** `system_stm32<family>xx.c`.

```cmake
cmake_minimum_required(VERSION 3.19)

target_include_directories(${PROJECT_NAME} PRIVATE Inc)

target_sources(${PROJECT_NAME} PRIVATE
    Src/stm32<family>xx_hal_msp.c
    Src/stm32<family>xx_it.c
    Src/<periph>.c          # по списку MX_*_Init() из main.c
    Src/syscalls.c
    Src/sysmem.c
    Src/main.c
)
```

`system_stm32<family>xx.c` и `startup_*.s` — в `stm32_config.yml` → `sources`.

### 3.3. `User/CMakeLists.txt`

```cmake
cmake_minimum_required(VERSION 3.19)

target_sources(${PROJECT_NAME} PRIVATE
    Src/program.cpp
)
```

При декомпозиции или наличии заголовков расширить:

```cmake
cmake_minimum_required(VERSION 3.19)

target_include_directories(${PROJECT_NAME} PRIVATE Inc)

target_sources(${PROJECT_NAME} PRIVATE
    Src/program.cpp
    Src/<module>.cpp
)
```

### 3.4. `stm32_config.yml`

Применять навык `stm32-config-manager`. Базовый шаблон:

```yaml
stm32_cmake_yml_version: "0.8"
stm32_cmake_yml_version_check: false

ioc_file: "<project>.ioc"
mcu: <MCU>           # опционально, если совпадает с .ioc

sources:
  - "Core"
  - "User"
  - "Core/Src/system_stm32<family>xx.c"
  - "startup_stm32<chip>x<density>.s"

hal_components: [ CORTEX, FLASH, RCC, GPIO, PWR ]
# добавить по IP из .ioc — см. references/MAPPINGS.md

languages: [C, CXX, ASM]
c_standard: 17
cpp_standard: 17
compile_options:
  - "O0 g3"
linker_directives:
  - "--print-memory-usage"
use_newlib_nano: true
system_library: "NoSys"
build_artifacts: [ bin, hex, map, lss ]
validate_linker_script: true
log_target_properties: false
verbose_build: false
```

### 3.5. `STM32<CHIP>XX_FLASH.ld.in`

Взять `.ld` из CubeMX и внести **только два изменения**:

```ld
/* было */
_Min_Heap_Size = 0x200;
_Min_Stack_Size = 0x400;

/* стало */
_Min_Heap_Size = @HEAP_SIZE@;
_Min_Stack_Size = @STACK_SIZE@;
```

Все вхождения `(READONLY)` заменить на `@USE_READONLY@`. Больше ничего не менять.

### 3.6. SVD-файл

Имя — **без** `XX` (например `STM32F411.svd`). Положить в корень проекта.

Ссылка для скачивания:
```
https://github.com/modm-io/cmsis-svd-stm32/blob/main/stm32<family>/STM32<CHIP>.svd
```

### 3.7. `.vscode/`

Все файлы кроме `settings.json` идентичны для всех примеров.
Шаблоны — в [assets/vscode/](assets/vscode/).

`settings.json` специфичен для каждого проекта. Подставить:
- `<MCU_DEVICE>` — например `STM32F411CEU6`
- `<SVD_FILE>` — например `STM32F411.svd`
- `<TARGET_FAMILY>` — например `stm32f4x`
- `<FAMILY_DEFINE>` — например `STM32F411xx` (в `c_cpp_properties.json`)

Полные шаблоны всех `.vscode/` файлов — в [references/VSCODE.md](references/VSCODE.md).

### 3.8. `User/Src/program.cpp`

Применять навык `cpp-clang-format`. Структура — Arduino-стиль.

**Правила шапки:** только назначение примера — что делает и как проверить.
Без `\file`, без даты, без автора.

**Правила кода:**
- Doxygen `/*!` для функций и переменных модульного уровня
- `///` для однострочных комментариев к членам
- `extern "C"` не нужен — `init/setup/loop` объявлены в `main.h`
- Дескрипторы периферии объявлять напрямую без обёрток

```cpp
/*!
 * \brief   <Что демонстрирует пример>.
 *
 * \details <Параметры, как проверить на анализаторе/терминале>.
 *
 * \board   WeAct BlackPill (STM32F411CEU6)
 */

#include "main.h"

// Внешние дескрипторы периферии, инициализированные в main.c (CubeMX).
extern <Handle_TypeDef> h<periph>;

// ---------------------------------------------------------------------------

/*!
 * \brief Инициализация до SystemClock_Config().
 */
void init( void )
{
    // Резервировано.
}

/*!
 * \brief Настройка после инициализации периферии CubeMX.
 */
void setup( void )
{
}

/*!
 * \brief Тело основного цикла.
 */
void loop( void )
{
}
```

### 3.9. Правки `main.h` и `main.c`

AI вносит изменения напрямую.

**`Core/Inc/main.h`** — в `USER CODE BEGIN EFP`:
```c
void init( void );
void setup( void );
void loop( void );
```

**`Core/Src/main.c`** — структура `main()`:
```c
HAL_Init();
/* USER CODE BEGIN Init */
init();
/* USER CODE END Init */

SystemClock_Config();
/* MX_*_Init() */

/* USER CODE BEGIN WHILE */
setup();
while (1) {
    loop();
}
```

### 3.10. `.clang-format`

Скопировать [assets/_clang-format](assets/_clang-format) в корень проекта под именем `.clang-format`.

---

## Шаг 4. Чеклист

| # | Действие | Кто |
|---|---|---|
| 1 | CubeMX: настроить, сгенерировать (Makefile + Reference mode) | Пользователь |
| 2 | Приложить `.ioc`, `main.c`, `main.h`; сообщить тему | Пользователь |
| 3 | Прочитать файлы, извлечь параметры | AI |
| 4 | `CMakeLists.txt` (корень) | AI |
| 5 | `Core/CMakeLists.txt` | AI |
| 6 | `User/CMakeLists.txt` | AI |
| 7 | `stm32_config.yml` | AI |
| 8 | `.vscode/` (все файлы) | AI |
| 9 | `STM32<CHIP>XX_FLASH.ld.in` | AI |
| 10 | Сообщить ссылку на SVD | AI |
| 11 | `User/Src/program.cpp` | AI |
| 12 | Правки `main.h` и `main.c` | AI |
| 13 | `CMake: build` — проверить сборку | Пользователь |
