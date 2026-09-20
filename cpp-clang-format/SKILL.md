---
name: cpp-clang-format
description: Применяет настройки форматирования кода C/C++ через .clang-format. Используйте для приведения стиля кода к единому стандарту проекта (140 символов, отступы 4 пробела, Allman-подобные скобки, пробелы в круглых скобках, отступ в namespace).
license: MIT
compatibility: Требуется clang-format 16+ (из-за SeparateDefinitionBlocks). Минимум 14 при удалении этого параметра. Стандарт парсинга C++11.
metadata:
  version: "0.5"
  style: Custom (140 cols, 4 spaces, spaces in parentheses)
---

# Навык форматирования C/C++ кода

Этот навык описывает правила применения конфигурации `.clang-format` для проектов на C и C++. Конфигурация является **"Единственным Источником Правды"** для стиля кода.

## Основные правила

| Правило | Описание |
|---------|----------|
| **Файл конфигурации** | `.clang-format` должен находиться в корневой директории проекта |
| **Применение** | `clang-format -i <файл>` — форматирование файла на месте |
| **Проверка** | `clang-format --dry-run <файл>` — проверка стиля без изменений |
| **Проверка для CI** | `clang-format --dry-run --Werror <файл>` — ненулевой код возврата при расхождении |
| **Просмотр расхождений** | `clang-format <файл> \| diff <файл> -` — показывает, что именно изменится |
| **Эффективный конфиг** | `clang-format --dump-config` — итоговые значения всех параметров |
| **Интеграция с IDE** | Настройки автоматически подхватываются VS Code, CLion, Visual Studio |

Рекурсивное применение ко всем исходникам:

```bash
find . -name "*.cpp" -o -name "*.h" -o -name "*.c" | xargs clang-format -i
```

Исключение сторонних папок (`Arduino`, `modules`, сгенерированный код):

```bash
find . -path ./modules -prune -o -path ./Arduino -prune -o \
    \( -name "*.cpp" -o -name "*.h" \) -print | xargs clang-format -i
```

## Ключевые настройки стиля

### Базовые

| Параметр | Значение | Описание |
|----------|----------|----------|
| `Language` | `Cpp` | Язык форматирования |
| `Standard` | `Cpp11` | Стандарт языка для парсинга |
| `ColumnLimit` | `140` | Максимальная длина строки |
| `IndentWidth` | `4` | Отступы 4 пробела |
| `ContinuationIndentWidth` | `4` | Отступ для продолжений строк |
| `TabWidth` | `4` | Ширина табуляции |
| `UseTab` | `Never` | **Запрет** на использование символов табуляции |
| `AccessModifierOffset` | `-4` | `public:` и `private:` выносятся на уровень класса |

### Размещение скобок

Стиль близок к Allman, но задан через `Custom` — это обязательно для управления скобками лямбд, `case`-блоков и `namespace`.

| Параметр | Значение | Описание |
|----------|----------|----------|
| `BreakBeforeBraces` | `Custom` | Пользовательская логика расположения скобок |
| `BraceWrapping.AfterCaseLabel` | `true` | Скобка блока `case` на новой строке |
| `BraceWrapping.AfterClass` | `true` | Скобка классов на новой строке |
| `BraceWrapping.AfterControlStatement` | `true` | Скобка `if`, `for`, `while` на новой строке |
| `BraceWrapping.AfterEnum` | `true` | Скобка `enum` на новой строке |
| `BraceWrapping.AfterFunction` | `true` | Скобка функций на новой строке |
| `BraceWrapping.AfterNamespace` | `true` | Скобка `namespace` на новой строке |
| `BraceWrapping.AfterStruct` | `true` | Скобка структур на новой строке |
| `BraceWrapping.AfterUnion` | `true` | Скобка `union` на новой строке |
| `BraceWrapping.BeforeElse` | `true` | `else` на новой строке |
| `BraceWrapping.BeforeLambdaBody` | `true` | Тело лямбды с новой строки |

### Выравнивание

| Параметр | Значение | Описание |
|----------|----------|----------|
| `AlignAfterOpenBracket` | `DontAlign` | Перенесённые аргументы получают фиксированный отступ, а не выравнивание по скобке |
| `AlignOperands` | `true` | Выравнивание операндов бинарных операторов |
| `AlignTrailingComments` | `true` | Выравнивание комментариев в конце строки |
| `AlignArrayOfStructures` | `Left` | Выравнивание столбцов в массивах структур (clang-format 13+) |
| `AlignConsecutiveMacros` | `true` | Выравнивание последовательных `#define` |
| `AlignConsecutiveAssignments` | объект | Выравнивание `=`; поле `AlignCompound: true` включает также составные операторы |
| `AlignConsecutiveDeclarations` | `None` | **Отключено** — тип и имя разделяются одним пробелом |
| `AlignEscapedNewlines` | `Left` | Обратные слэши в многострочных макросах прижаты влево |
| `Cpp11BracedListStyle` | `false` | Списки инициализации с пробелами: `{ 1, 2 }` |

### Пробелы

| Параметр | Значение | Описание |
|----------|----------|----------|
| `SpacesInParentheses` | `true` | **Пробелы внутри круглых скобок** `( x )` |
| `SpacesInCStyleCastParentheses` | `true` | Пробелы в скобках C-style cast `( int )` |
| `SpaceAfterCStyleCast` | `true` | Пробел после C-style cast: `( int ) x` |
| `SpacesInSquareBrackets` | `false` | Без пробелов в индексах: `buf[i]` |
| `SpacesInAngles` | `false` | Без пробелов в шаблонах: `vector<int>` |
| `SpaceInEmptyParentheses` | `false` | Пустые скобки без пробела: `func()` |
| `SpaceBeforeParens` | `ControlStatements` | Пробел перед `(` только у управляющих конструкций |
| `SpaceBeforeAssignmentOperators` | `true` | Пробел перед `=` |
| `SpaceBeforeCtorInitializerColon` | `true` | Пробел перед двоеточием списка инициализации |
| `SpaceBeforeInheritanceColon` | `true` | Пробел перед двоеточием наследования |
| `SpacesBeforeTrailingComments` | `4` | Четыре пробела перед комментарием в конце строки |
| `SpaceAfterTemplateKeyword` | `false` | `template<...>` без пробела |
| `SpaceAfterLogicalNot` | `false` | `!flag` без пробела |

### Переносы и упаковка

| Параметр | Значение | Описание |
|----------|----------|----------|
| `BinPackArguments` | `true` | Упаковка аргументов при вызове функции |
| `BinPackParameters` | `true` | Упаковка параметров функции |
| `AllowAllArgumentsOnNextLine` | `false` | Запрет переноса всех аргументов на следующую строку |
| `AllowAllParametersOfDeclarationOnNextLine` | `true` | Разрешён перенос всех параметров |
| `AllowShortFunctionsOnASingleLine` | `InlineOnly` | **Однострочные методы внутри классов сохраняются**, свободные функции разворачиваются |
| `AllowShortBlocksOnASingleLine` | `false` | Короткие блоки разворачиваются |
| `AllowShortCaseLabelsOnASingleLine` | `false` | Короткие `case` разворачиваются |
| `AllowShortIfStatementsOnASingleLine` | `true` | Короткий `if` **разрешён** на одной строке |
| `AllowShortEnumsOnASingleLine` | `true` | Короткий `enum` разрешён на одной строке |
| `BreakBeforeBinaryOperators` | `None` | Перенос после оператора, не перед ним |
| `BreakBeforeTernaryOperators` | `true` | Перенос перед знаками тернарного оператора |
| `BreakConstructorInitializers` | `AfterColon` | Двоеточие остаётся на строке конструктора |
| `BreakInheritanceList` | `AfterColon` | Аналогично для списка наследования |
| `AlwaysBreakTemplateDeclarations` | `Yes` | `template<...>` всегда на отдельной строке |
| `PenaltyReturnTypeOnItsOwnLine` | `100` | Штраф за вынос возвращаемого типа на отдельную строку |

### Отступы конструкций

| Параметр | Значение | Описание |
|----------|----------|----------|
| `IndentCaseLabels` | `true` | Метки `case` с отступом от `switch` |
| `IndentCaseBlocks` | `false` | Блок после `case` **без** дополнительного уровня отступа |
| `IndentGotoLabels` | `false` | Метки `goto` прижаты влево |
| `IndentPPDirectives` | `None` | Директивы препроцессора без отступа |
| `IndentWrappedFunctionNames` | `false` | Перенесённые имена функций без отступа |
| `NamespaceIndentation` | `All` | **Отступ содержимого на всех уровнях** `namespace` |
| `CompactNamespaces` | `false` | Вложенные `namespace` не схлопываются в одну строку |
| `ConstructorInitializerIndentWidth` | `4` | Отступ списка инициализации конструктора |

### Указатели, include, прочее

| Параметр | Значение | Описание |
|----------|----------|----------|
| `PointerAlignment` | `Left` | `int* ptr`, а не `int *ptr` |
| `ReferenceAlignment` | `Left` | `int& ref` |
| `IncludeBlocks` | не задан | Перегруппировка отключена (требует `SortIncludes: true`) |
| `SortIncludes` | `false` | **Сортировка отключена** — порядок include не меняется |
| `SortUsingDeclarations` | `true` | Сортировка `using`-деклараций |
| `MaxEmptyLinesToKeep` | `2` | Максимум 2 пустые строки подряд |
| `KeepEmptyLinesAtTheStartOfBlocks` | `false` | Пустая строка сразу после открывающей скобки удаляется |
| `SeparateDefinitionBlocks` | `Always` | Пустая строка между определениями (clang-format 16+) |
| `FixNamespaceComments` | `false` | Комментарий у закрывающей скобки `namespace` **не добавляется** |
| `ReflowComments` | `false` | Длинные комментарии не переносятся |
| `ExperimentalAutoDetectBinPacking` | `false` | Автоопределение упаковки отключено |

## Примеры форматирования

### Базовое форматирование

До:

```cpp
void process_data(int* buffer,size_t length){
if(length==0)return;
for(size_t i=0;i<length;i++){
buffer[i]=(uint8_t)(buffer[i]*2);
}
}

#define MAX_VALUE 100
#define MIN_VALUE 0
#define DEFAULT_VALUE 50
```

После:

```cpp
void process_data( int* buffer, size_t length )
{
    if ( length == 0 ) return;

    for ( size_t i = 0; i < length; i++ )
    {
        buffer[i] = ( uint8_t ) ( buffer[i] * 2 );
    }
}

#define MAX_VALUE     100
#define MIN_VALUE     0
#define DEFAULT_VALUE 50
```

### Пространства имён

`AfterNamespace: true` выносит скобку на новую строку, `NamespaceIndentation: All`
добавляет отступ содержимому, `FixNamespaceComments: false` не дописывает
комментарий к закрывающей скобке.

```cpp
namespace ParamConsts
{
    /// "PARM" в ASCII.
    constexpr uint32_t EEPROM_MAGIC = 0x5041524D;

    /// Адрес для хранения магического числа.
    constexpr uint16_t EEPROM_MAGIC_ADDR  = 0;
    constexpr uint16_t EEPROM_VALUES_ADDR = EEPROM_MAGIC_ADDR + sizeof( EEPROM_MAGIC );

    /// 1 байт для типа (variant index) + 31 байт для строки. Всего 32 байта.
    constexpr uint16_t EEPROM_PARAM_SLOT_SIZE = 1 + PARAM_STRING_MAX_LEN;
}
```

Обратите внимание на выравнивание `EEPROM_MAGIC_ADDR` и `EEPROM_VALUES_ADDR` в одну
колонку: это работа `AlignConsecutiveAssignments`. Поскольку `AcrossComments: false`,
группа выравнивания разрывается на комментарии — `EEPROM_MAGIC` выше и
`EEPROM_PARAM_SLOT_SIZE` ниже выравниваются независимо.

### Конструкция switch

При `IndentCaseBlocks: false` блок остаётся на уровне метки `case`:

```cpp
switch ( static_cast<ParamType>( typeTag ) )
{
    case ParamType::INT:
    {
        int64_t storedValue = 0;
        readBuffered( param.eeprom_addr + 1, storedValue );

        if ( isIntValueValid( param, storedValue ) )
        {
            param.value = storedValue;
        }
        else
        {
            param.value = param.def;
        }

        break;
    }

    case ParamType::BOOL:
    {
        bool storedValue = false;
        readBuffered( param.eeprom_addr + 1, storedValue );
        param.value = storedValue;
        break;
    }

    default:
        param.value = param.def;
        break;
}
```

### Короткие методы классов

`AllowShortFunctionsOnASingleLine: InlineOnly` сохраняет компактными геттеры
и предикаты, объявленные внутри класса:

```cpp
class FsmManager
{
public:
    bool isCombatActive() const { return currentState == SystemState::ACTIVE_FIRING; }
    uint32_t getUptime() const { return uptimeSeconds; }

    void update();

private:
    SystemState currentState = SystemState::IDLE;
    uint32_t uptimeSeconds   = 0;
};
```

Определение вне класса по-прежнему разворачивается:

```cpp
void FsmManager::update()
{
    uptimeSeconds++;
}
```

### Лямбда-выражения

`BraceWrapping.BeforeLambdaBody: true` выносит тело на новую строку:

```cpp
auto printRow16 = [&]( const char* name, uint8_t reg )
{
    uint16_t value = readRegister16( reg );
    Serial.print( name );
    Serial.println( value, HEX );
};
```

### Массивы структур

`AlignArrayOfStructures: Left` выравнивает столбцы, `Cpp11BracedListStyle: false`
добавляет пробелы внутри фигурных скобок:

```cpp
const struct
{
    uint32_t flag;
    const char* name;
} reset_flags[] = {
    { RCC_CSR_BORRSTF,  "BOR"  },    // Brown-Out Reset
    { RCC_CSR_PINRSTF,  "PIN"  },    // Reset from NRST pin
    { RCC_CSR_SFTRSTF,  "SFT"  },    // Software Reset
    { RCC_CSR_IWDGRSTF, "IWDG" },    // Independent Watchdog Reset
    { RCC_CSR_WWDGRSTF, "WWDG" },    // Window Watchdog Reset
};
```

### Классы и наследование

`AccessModifierOffset: -4` выносит спецификаторы на уровень `class`,
`BreakInheritanceList: AfterColon` оставляет двоеточие на строке класса.

```cpp
class OfbchPayload : public PayloadDriver
{
public:
    OfbchPayload( Bsp& bsp, uint8_t channel ) :
        PayloadDriver( bsp ), channelIndex( channel )
    {
    }

    void arm() override;

private:
    uint8_t channelIndex;
};
```

### Особенности стиля

| Особенность | Пример |
|-------------|--------|
| **Пробелы в круглых скобках** | `func( arg1, arg2 )` |
| **Без пробелов в квадратных** | `buffer[i]` |
| **Пробелы в списках инициализации** | `{ 1, 2, 3 }` |
| **Скобки на новой строке** | `void func()` затем `{` |
| **Выравнивание присваиваний** | `x  = 10;` и `yy = 20;` |
| **Пробел после cast** | `( int ) value` |
| **else на новой строке** | `}` затем `else` затем `{` |
| **Короткий if на одной строке** | `if ( !ptr ) return;` |
| **Inline-методы классов** | `bool isReady() const { return ready; }` |
| **Отступ в namespace** | содержимое смещено на 4 пробела |

## Локальное отключение форматирования

Для блоков с ручным выравниванием, которые clang-format переформатирует нежелательным образом (таблицы регистров, ASCII-схемы, выровненные инициализаторы):

```cpp
// clang-format off
const uint8_t lookup[] = {
    0x00, 0x01, 0x03, 0x07,
    0x0F, 0x1F, 0x3F, 0x7F,
};
// clang-format on
```

## Известные ограничения и замечания

| Тема | Описание |
|------|----------|
| **Скобка инициализатора** | clang-format не умеет безусловно переносить открывающую скобку списка инициализации на новую строку — переносит только при превышении `ColumnLimit`. Для записи вида `reset_flags[] =` с последующей скобкой на отдельной строке используйте `// clang-format off` |
| **Отступ вложенных namespace** | `NamespaceIndentation: All` даёт кумулятивный отступ: на третьем уровне вложенности код смещается на 12 пробелов и съедает `ColumnLimit` |
| **AllowAllConstructorInitializersOnNextLine** | Устарел в clang-format 15+, заменён на `PackConstructorInitializers`. Пока принимается, но выдаёт предупреждение в новых версиях |
| **Standard: Cpp11** | Устаревшее написание, современный эквивалент — `c++11` |
| **BOM в файле** | `.clang-format` содержит BOM. На работу не влияет, но может мешать diff-инструментам |
| **LineEnding** | Параметр закомментирован. При смешанных CRLF и LF в репозитории задайте `LineEnding: LF` (16+) или пару `DeriveLineEnding: false` и `UseCRLF: false` (14–15), а также добавьте `.gitattributes` со строкой `* text=auto eol=lf` |
| **Комментарии в конфиге** | Несколько комментариев разошлись со значениями: `AllowShortIfStatementsOnASingleLine` описан как «запретить», но установлен в `true`; `IndentPPDirectives` снабжён комментарием про `case`; `FixNamespaceComments` описан как «добавлять», но установлен в `false` |

## Проверка версии clang-format

Конфигурация использует параметры разных версий:

| Параметр | Минимальная версия |
|----------|--------------------|
| `IndentCaseBlocks`, `BeforeLambdaBody`, `SpaceBeforeLambdaBody` | 11 |
| `AlignArrayOfStructures` | 13 |
| `AlignConsecutiveAssignments` в объектной форме | 14 |
| `SeparateDefinitionBlocks` | 16 |

```bash
clang-format --version
```

Если доступна только версия ниже 16, удалите `SeparateDefinitionBlocks` — остальные параметры работают начиная с 14. Неизвестный параметр вызывает ошибку разбора конфигурации, а не игнорируется.
