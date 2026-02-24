---
name: stm32-bsp-driver-pattern
description: Стандартный шаблон драйверов внешних устройств для STM32 в стиле BSP от ST. Используйте для создания новых драйверов датчиков/микросхем, рефакторинга существующего кода или создания унифицированного интерфейса к периферии.
license: MIT
compatibility: Требуется фреймворк stm32-cmake-yml. Совместим с драйверами из STM32Cube_FW (Drivers/BSP/Components).
metadata:
  version: "0.1"
  pattern_source: STM32Cube_FW BSP Components
---

# Шаблон драйверов внешних устройств STM32 (BSP Pattern)

Этот навык описывает **стандартизированный подход ST** к созданию драйверов внешних устройств (датчиков, дисплеев, памяти и т.д.). Паттерн обеспечивает единообразие, переиспользуемость и лёгкую интеграцию с проектами на базе stm32-cmake-yml.

## ⚠️ Когда использовать этот навык

| Сценарий | Рекомендация |
| --- | --- |
| Создание нового драйвера по datasheet | ✅ **Этот навык** |
| Рефакторинг существующего драйвера под BSP-стиль | ✅ **Этот навык** |
| Обёртка существующего кода без переписывания | ✅ **Этот навык** |
| Простой код для одного устройства (без переиспользования) | ❌ Используйте `stm32-simple-sources` |
| Драйвер встроенной периферии (GPIO, UART внутри MCU) | ❌ Используйте HAL/LL напрямую |

---

## Архитектурные принципы

### 1. Разделение уровней (Layer Separation)

```
┌─────────────────────────────────────────────────────────────┐
│                    Приложение (Application)                 │
│                     main.c, user_code.c                     │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                  Драйвер устройства (Component)             │
│              l3gd20.c/h, accelero.c/h, io.c/h               │
│   • Логика работы с устройством                             │
│   • Конфигурация регистров                                  │
│   • Не зависит от способа подключения (I2C/SPI)             │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              IO Abstraction Layer (Порт ввода-вывода)       │
│              gyro_io.c/h, sensor_io.c/h                     │
│   • GYRO_IO_Init(), GYRO_IO_Write(), GYRO_IO_Read()         │
│   • Конкретная реализация I2C/SPI/UART для вашей платы      │
│   • Единственное место для изменения при смене платформы    │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    HAL / LL драйверы STM32                  │
│              stm32f4xx_hal_i2c.c, stm32f4xx_hal_spi.c       │
└─────────────────────────────────────────────────────────────┘
```

**Ключевое преимущество:** Драйвер устройства **не знает**, как физически подключен чип. Это позволяет использовать один и тот же драйвер на разных платах.

---

## Структура файлов драйвера

### Обязательные файлы

| Файл | Назначение |
| --- | --- |
| `{device}.h` | Заголовок: определения регистров, структуры, прототипы функций |
| `{device}.c` | Реализация: логика работы с устройством |
| `{device}_io.h` | Заголовок IO-уровня: прототипы функций ввода-вывода |
| `{device}_io.c` | Реализация IO-уровня: I2C/SPI/UART для вашей платы |

### Рекомендуемая структура папок

```
Project/
├── Drivers/
│   └── BSP/
│       └── Components/
│           └── {device_name}/
│               ├── {device}.h
│               ├── {device}.c
│               ├── {device}_io.h
│               └── {device}_io.c
├── Core/
│   └── Inc/
│       └── stm32f4xx_hal_conf.h
└── stm32_config.yml
```

---

## 1. Заголовок драйвера ({device}.h)

### 1.1 Стандартные элементы

```c
/* Define to prevent recursive inclusion -------------------------------------*/
#ifndef __{DEVICE}_H
#define __{DEVICE}_H

#ifdef __cplusplus
extern "C" {
#endif

/* Includes ------------------------------------------------------------------*/
#include <stdint.h>
#include <stdbool.h>

/** @addtogroup BSP
@{
*/

/** @addtogroup Components
@{
*/

/** @addtogroup {DEVICE_NAME}
@{
*/
```

### 1.2 Карта регистров (Register Mapping)

```c
/** @defgroup {DEVICE}_Exported_Constants
@{
*/

/*************************** START REGISTER MAPPING  **************************/
#define {DEVICE}_WHO_AM_I_ADDR          0x0F  /**< Device identification register */
#define {DEVICE}_CTRL_REG1_ADDR         0x20  /**< Control register 1 */
#define {DEVICE}_CTRL_REG2_ADDR         0x21  /**< Control register 2 */
#define {DEVICE}_OUT_X_L_ADDR           0x28  /**< Output Register X Low */
#define {DEVICE}_OUT_X_H_ADDR           0x29  /**< Output Register X High */
/*************************** END REGISTER MAPPING  ****************************/

/** @defgroup {DEVICE}_Device_ID
@{
*/
#define I_AM_{DEVICE}                   ((uint8_t)0xD4)
#define I_AM_{DEVICE}_TR                ((uint8_t)0xD5)
/**
@}
*/
```

### 1.3 Конфигурационные определения

```c
/** @defgroup {DEVICE}_Power_Mode
@{
*/
#define {DEVICE}_MODE_POWERDOWN         ((uint8_t)0x00)
#define {DEVICE}_MODE_ACTIVE            ((uint8_t)0x08)
/**
@}
*/

/** @defgroup {DEVICE}_Full_Scale
@{
*/
#define {DEVICE}_FULLSCALE_250          ((uint8_t)0x00)
#define {DEVICE}_FULLSCALE_500          ((uint8_t)0x10)
#define {DEVICE}_FULLSCALE_2000         ((uint8_t)0x20)
/**
@}
*/

/** @defgroup {DEVICE}_Sensitivity
@{
*/
#define {DEVICE}_SENSITIVITY_250DPS     ((float)8.75f)
#define {DEVICE}_SENSITIVITY_500DPS     ((float)17.50f)
#define {DEVICE}_SENSITIVITY_2000DPS    ((float)70.00f)
/**
@}
*/
```

### 1.4 Структура инициализации

```c
/** @defgroup {DEVICE}_Configuration_structure
@{
*/
typedef struct
{
    uint8_t Power_Mode;                 /**< Power-down/Normal Mode */
    uint8_t Output_DataRate;            /**< OUT data rate */
    uint8_t Axes_Enable;                /**< Axes enable */
    uint8_t Full_Scale;                 /**< Full Scale selection */
    uint8_t Bandwidth;                  /**< Bandwidth selection */
} {DEVICE}_InitTypeDef;
/**
@}
*/
```

### 1.5 Структура драйвера (Driver Structure)

**Это ключевой элемент BSP-паттерна!** Обеспечивает единый интерфейс для всех устройств одного типа.

```c
/** @defgroup {DEVICE}_Driver_structure
@{
*/
typedef struct
{
    void       (*Init)(uint16_t);
    void       (*DeInit)(void);
    uint8_t    (*ReadID)(void);
    void       (*Reset)(void);
    void       (*LowPower)(uint16_t);
    void       (*ConfigIT)(uint16_t);
    void       (*EnableIT)(uint8_t);
    void       (*DisableIT)(uint8_t);
    uint8_t    (*ITStatus)(uint16_t);
    void       (*ClearIT)(void);
    void       (*FilterConfig)(uint8_t);
    void       (*FilterCmd)(uint8_t);
    void       (*ReadXYZ)(float *);
} {DEVICE}_DrvTypeDef;

/**
@}
*/

/** @defgroup {DEVICE}_Exported_Variables
@{
*/
extern {DEVICE}_DrvTypeDef {Device}Drv;
/**
@}
*/
```

### 1.6 Прототипы функций

```c
/** @defgroup {DEVICE}_Exported_Functions
@{
*/

/* Sensor Configuration Functions */
void    {DEVICE}_Init(uint16_t InitStruct);
void    {DEVICE}_DeInit(void);
uint8_t {DEVICE}_ReadID(void);
void    {DEVICE}_Reset(void);
void    {DEVICE}_LowPower(uint16_t InitStruct);

/* Interrupt Configuration Functions */
void    {DEVICE}_EnableIT(uint8_t IntSel);
void    {DEVICE}_DisableIT(uint8_t IntSel);
uint8_t {DEVICE}_ITStatus(uint16_t Instance);
void    {DEVICE}_ClearIT(void);

/* Data Reading Functions */
void    {DEVICE}_ReadXYZ(float *pData);
uint8_t {DEVICE}_GetDataStatus(void);

/* IO Functions (должны быть реализованы в {device}_io.c) */
void    {DEVICE}_IO_Init(void);
void    {DEVICE}_IO_DeInit(void);
void    {DEVICE}_IO_Write(uint8_t *pBuffer, uint8_t WriteAddr, uint16_t NumByteToWrite);
void    {DEVICE}_IO_Read(uint8_t *pBuffer, uint8_t ReadAddr, uint16_t NumByteToRead);

/**
@}
*/
```

### 1.7 Завершение заголовка

```c
/**
@}
*/

/**
@}
*/

/**
@}
*/

#ifdef __cplusplus
}
#endif

#endif /* __{DEVICE}_H */
```

---

## 2. Реализация драйвера ({device}.c)

### 2.1 Экземпляр структуры драйвера

```c
/** @addtogroup {DEVICE}_Private_Variables
@{
*/
{DEVICE}_DrvTypeDef {Device}Drv =
{
    {DEVICE}_Init,
    {DEVICE}_DeInit,
    {DEVICE}_ReadID,
    {DEVICE}_Reset,
    {DEVICE}_LowPower,
    {DEVICE}_INT1InterruptConfig,
    {DEVICE}_EnableIT,
    {DEVICE}_DisableIT,
    0,  /* Не все функции обязательны */
    0,
    {DEVICE}_FilterConfig,
    {DEVICE}_FilterCmd,
    {DEVICE}_ReadXYZ
};
/**
@}
*/
```

### 2.2 Пример функции инициализации

```c
/**
@brief  Set {DEVICE} Initialization.
@param  InitStruct: pointer to a {DEVICE}_InitTypeDef structure
    that contains the configuration setting for the {DEVICE}.
@retval None
*/
void {DEVICE}_Init(uint16_t InitStruct)
{
    uint8_t ctrl = 0x00;

    /* Configure the low level interface */
    {DEVICE}_IO_Init();

    /* Write value to MEMS CTRL_REG1 register */
    ctrl = (uint8_t) InitStruct;
    {DEVICE}_IO_Write(&ctrl, {DEVICE}_CTRL_REG1_ADDR, 1);

    /* Write value to MEMS CTRL_REG4 register */
    ctrl = (uint8_t) (InitStruct >> 8);
    {DEVICE}_IO_Write(&ctrl, {DEVICE}_CTRL_REG4_ADDR, 1);
}
```

### 2.3 Пример функции чтения данных

```c
/**
@brief  Calculate the {DEVICE} angular data.
@param  pData: Data out pointer
@retval None
*/
void {DEVICE}_ReadXYZ(float *pData)
{
    uint8_t tmpbuffer[6] = {0};
    int16_t RawData[3] = {0};
    uint8_t tmpreg = 0;
    float sensitivity = 0;
    int i = 0;

    /* Read control register to determine sensitivity */
    {DEVICE}_IO_Read(&tmpreg, {DEVICE}_CTRL_REG4_ADDR, 1);

    /* Read 6 bytes of data (X, Y, Z - Low and High) */
    {DEVICE}_IO_Read(tmpbuffer, {DEVICE}_OUT_X_L_ADDR, 6);

    /* Check endianness and combine bytes */
    if(!(tmpreg & {DEVICE}_BLE_MSB))
    {
        for(i = 0; i < 3; i++)
        {
            RawData[i] = (int16_t)(((uint16_t)tmpbuffer[2*i+1] << 8) + tmpbuffer[2*i]);
        }
    }
    else
    {
        for(i = 0; i < 3; i++)
        {
            RawData[i] = (int16_t)(((uint16_t)tmpbuffer[2*i] << 8) + tmpbuffer[2*i+1]);
        }
    }

    /* Select sensitivity based on full scale */
    switch(tmpreg & {DEVICE}_FULLSCALE_SELECTION)
    {
        case {DEVICE}_FULLSCALE_250:
            sensitivity = {DEVICE}_SENSITIVITY_250DPS;
            break;
        case {DEVICE}_FULLSCALE_500:
            sensitivity = {DEVICE}_SENSITIVITY_500DPS;
            break;
        case {DEVICE}_FULLSCALE_2000:
            sensitivity = {DEVICE}_SENSITIVITY_2000DPS;
            break;
    }

    /* Convert to physical units */
    for(i = 0; i < 3; i++)
    {
        pData[i] = (float)(RawData[i] * sensitivity);
    }
}
```

---

## 3. IO Abstraction Layer ({device}_io.c/h)

**Это самый важный файл для портируемости!** Здесь реализуется конкретный способ подключения (I2C, SPI, UART).

### 3.1 Заголовок IO-уровня

```c
#ifndef __{DEVICE}_IO_H
#define __{DEVICE}_IO_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

/* IO functions */
void    {DEVICE}_IO_Init(void);
void    {DEVICE}_IO_DeInit(void);
int8_t  {DEVICE}_IO_Write(uint8_t *pBuffer, uint8_t WriteAddr, uint16_t NumByteToWrite);
int8_t  {DEVICE}_IO_Read(uint8_t *pBuffer, uint8_t ReadAddr, uint16_t NumByteToRead);

/* Для SPI может потребоваться */
void    {DEVICE}_IO_WriteReg(uint8_t Reg, uint8_t Value);
uint8_t {DEVICE}_IO_ReadReg(uint8_t Reg);

#ifdef __cplusplus
}
#endif

#endif /* __{DEVICE}_IO_H */
```

### 3.2 Реализация для I2C

```c
#include "{device}_io.h"
#include "main.h"  /* Для_handles_ I2C */

extern I2C_HandleTypeDef hi2c1;
#define {DEVICE}_I2C_ADDR    ((uint16_t)0xD4)

void {DEVICE}_IO_Init(void)
{
    /* I2C уже инициализирован в CubeMX */
    /* Можно добавить проверку готовности устройства */
}

void {DEVICE}_IO_DeInit(void)
{
    /* HAL_I2C_DeInit(&hi2c1); */
}

int8_t {DEVICE}_IO_Write(uint8_t *pBuffer, uint8_t WriteAddr, uint16_t NumByteToWrite)
{
    if(HAL_I2C_Mem_Write(&hi2c1, {DEVICE}_I2C_ADDR,
                         WriteAddr, I2C_MEMADD_SIZE_8BIT,
                         pBuffer, NumByteToWrite, 100) == HAL_OK)
    {
        return 0;
    }
    return -1;
}

int8_t {DEVICE}_IO_Read(uint8_t *pBuffer, uint8_t ReadAddr, uint16_t NumByteToRead)
{
    if(HAL_I2C_Mem_Read(&hi2c1, {DEVICE}_I2C_ADDR,
                        ReadAddr, I2C_MEMADD_SIZE_8BIT,
                        pBuffer, NumByteToRead, 100) == HAL_OK)
    {
        return 0;
    }
    return -1;
}
```

### 3.3 Реализация для SPI

```c
#include "{device}_io.h"
#include "main.h"

extern SPI_HandleTypeDef hspi1;
#define {DEVICE}_CS_Port    GPIOA
#define {DEVICE}_CS_Pin     GPIO_PIN_4

void {DEVICE}_IO_Init(void)
{
    /* CS pin уже настроен в CubeMX */
}

int8_t {DEVICE}_IO_Write(uint8_t *pBuffer, uint8_t WriteAddr, uint16_t NumByteToWrite)
{
    uint8_t cmd[256];
    cmd[0] = WriteAddr & 0x7F;  /* Write bit = 0 */
    memcpy(&cmd[1], pBuffer, NumByteToWrite);

    HAL_GPIO_WritePin({DEVICE}_CS_Port, {DEVICE}_CS_Pin, GPIO_PIN_RESET);
    HAL_SPI_Transmit(&hspi1, cmd, NumByteToWrite + 1, 100);
    HAL_GPIO_WritePin({DEVICE}_CS_Port, {DEVICE}_CS_Pin, GPIO_PIN_SET);

    return 0;
}

int8_t {DEVICE}_IO_Read(uint8_t *pBuffer, uint8_t ReadAddr, uint16_t NumByteToRead)
{
    uint8_t cmd = ReadAddr | 0x80;  /* Read bit = 1 */

    HAL_GPIO_WritePin({DEVICE}_CS_Port, {DEVICE}_CS_Pin, GPIO_PIN_RESET);
    HAL_SPI_Transmit(&hspi1, &cmd, 1, 100);
    HAL_SPI_Receive(&hspi1, pBuffer, NumByteToRead, 100);
    HAL_GPIO_WritePin({DEVICE}_CS_Port, {DEVICE}_CS_Pin, GPIO_PIN_SET);

    return 0;
}
```

---

## 4. Интеграция с проектом

### 4.1 Добавление в stm32_config.yml

```yaml
sources:
  - "Core"
  - "Drivers/BSP/Components/l3gd20"  # ← Путь к драйверу

include_directories:
  - "."
  - "Core/Inc"
  - "Drivers/BSP/Components/l3gd20"  # ← Пути к заголовкам драйвера
```

### 4.2 Использование в приложении

```c
#include "l3gd20.h"

int main(void)
{
    HAL_Init();
    SystemClock_Config();
    MX_GPIO_Init();
    MX_I2C1_Init();

    /* Инициализация драйвера */
    L3GD20_Init(L3GD20_MODE_ACTIVE | L3GD20_FULLSCALE_500);

    /* Проверка ID устройства */
    if(L3GD20_ReadID() != I_AM_L3GD20)
    {
        /* Устройство не найдено */
        Error_Handler();
    }

    /* Чтение данных */
    float gyro_data[3];
    while(1)
    {
        L3GD20_ReadXYZAngRate(gyro_data);
        /* gyro_data[0] = X, gyro_data[1] = Y, gyro_data[2] = Z */
        HAL_Delay(10);
    }
}
```

### 4.3 Использование через структуру драйвера

```c
#include "l3gd20.h"

/* Доступ через структуру драйвера (BSP-стиль) */
extern GYRO_DrvTypeDef L3gd20Drv;

int main(void)
{
    /* Инициализация через структуру */
    L3gd20Drv.Init(L3GD20_MODE_ACTIVE);

    /* Чтение ID через структуру */
    uint8_t id = L3gd20Drv.ReadID();

    /* Чтение данных через структуру */
    float data[3];
    L3gd20Drv.ReadXYZAngRate(data);
}
```

---

## 5. Сценарии использования

### Сценарий 1: Новый драйвер по datasheet

**Шаги:**
1. Изучите datasheet устройства (регистры, биты, последовательности)
2. Создайте `{device}.h` с картой регистров и определениями
3. Создайте `{device}.c` с функциями инициализации и чтения
4. Создайте `{device}_io.c` для вашего интерфейса (I2C/SPI)
5. Добавьте в `stm32_config.yml`

**Пример для нового датчика температуры:**
```c
/* tsensor.h */
#define TSENSOR_ADDR            0x90
#define TSENSOR_CONFIG_REG      0x01
#define TSENSOR_TEMP_REG        0x00

typedef struct {
    uint8_t AlertMode;
    uint8_t ConversionRate;
    uint8_t Resolution;
} TSENSOR_InitTypeDef;

void TSENSOR_Init(uint16_t Instance, TSENSOR_InitTypeDef *Init);
uint16_t TSENSOR_ReadTemp(uint16_t Instance);
```

### Сценарий 2: Рефакторинг существующего кода

**Было:**
```c
/* Разрозненные функции без структуры */
void gyro_init(void);
uint8_t gyro_read_id(void);
void gyro_read_data(float *data);
```

**Стало (BSP-стиль):**
```c
/* Единая структура драйвера */
typedef struct {
    void (*Init)(uint16_t);
    uint8_t (*ReadID)(void);
    void (*ReadXYZ)(float *);
} GYRO_DrvTypeDef;

extern GYRO_DrvTypeDef L3gd20Drv;

/* Использование */
L3gd20Drv.Init(config);
L3gd20Drv.ReadXYZ(data);
```

### Сценарий 3: Обёртка для существующего кода

Если у вас уже есть рабочий драйвер без BSP-структуры:

```c
/* wrapper.c */
#include "legacy_driver.h"
#include "l3gd20.h"

/* Обёртка над старыми функциями */
static void Wrapper_Init(uint16_t InitStruct) {
    legacy_gyro_init();
}

static uint8_t Wrapper_ReadID(void) {
    return legacy_gyro_read_id();
}

static void Wrapper_ReadXYZ(float *pData) {
    legacy_gyro_get_data(pData);
}

/* Экспорт структуры драйвера */
GYRO_DrvTypeDef L3gd20Drv = {
    Wrapper_Init,
    Wrapper_DeInit,
    Wrapper_ReadID,
    /* ... */
};
```

---

## 6. Контрольный список

### Заголовок ({device}.h)
- [ ] Header guards (`#ifndef __{DEVICE}_H`)
- [ ] `extern "C"` для C++ совместимости
- [ ] Карта регистров с адресами
- [ ] Определения битов и значений
- [ ] Структура инициализации (`_InitTypeDef`)
- [ ] Структура драйвера (`_DrvTypeDef`)
- [ ] Прототипы всех функций
- [ ] Doxygen-комментарии (`@brief`, `@param`, `@retval`)

### Реализация ({device}.c)
- [ ] Экземпляр структуры драйвера
- [ ] Функция инициализации
- [ ] Функция чтения ID
- [ ] Функции чтения данных
- [ ] Функции управления (Reset, LowPower)
- [ ] Обработка ошибок

### IO-уровень ({device}_io.c)
- [ ] Функции Init/DeInit
- [ ] Функции Write/Read
- [ ] Конкретная реализация I2C/SPI/UART
- [ ] Обработка ошибок возврата

### Интеграция
- [ ] Добавлено в `sources` в `stm32_config.yml`
- [ ] Добавлено в `include_directories`
- [ ] Проверена компиляция
- [ ] Проверена работа на железе

---

## 7. Частые ошибки и решения

| Ошибка | Причина | Решение |
| --- | --- | --- |
| `undefined reference to {DEVICE}_IO_Write` | Не добавлен `{device}_io.c` в сборку | Добавьте папку в `sources` в `stm32_config.yml` |
| Устройство не отвечает на I2C | Неправильный адрес | Проверьте адрес в datasheet (7-bit vs 8-bit) |
| Неправильные данные | Endianness (порядок байт) | Проверьте бит BLE в регистре управления |
| Драйвер не компилируется | Нет `extern "C"` | Добавьте `#ifdef __cplusplus` guards |
| Конфликт имён функций | Одинаковые имена в разных драйверах | Используйте префикс `{DEVICE}_` для всех функций |

---

## 8. Ссылки на примеры

| Устройство | Файлы | Интерфейс |
| --- | --- | --- |
| L3GD20 (Гироскоп) | `l3gd20.h`, `l3gd20.c` | SPI/I2C |
| LSM303DLHC (Акселерометр) | `accelero.h` | I2C |
| LM75 (Температура) | `tsensor.h` | I2C |
| MFX (IO Expander) | `io.h` | I2C |

---

## 9. Примечания

1. **Префиксы имён:** Все функции, определения и структуры должны использовать префикс устройства (`L3GD20_`, `ACCELERO_`, `TSENSOR_`).

2. **Структура драйвера:** Не все поля `_DrvTypeDef` обязательны. Используйте `0` для неиспользуемых функций.

3. **IO-уровень:** Это единственное место, которое нужно менять при переносе на другую плату.

4. **Doxygen:** Используйте единый стиль комментариев для генерации документации.

5. **Лицензирование:** Сохраняйте copyright-заголовки ST в файлах из Cube_FW. Для новых драйверов укажите свою лицензию.

6. **Интеграция с FreeRTOS:** При использовании RTOS добавьте мьютексы в IO-функции для защиты шины I2C/SPI.

---

## 10. Связанные навыки

- [stm32-config-manager](../stm32-config-manager/SKILL.md) — Добавление драйвера в конфигурацию проекта
- [stm32-simple-sources](../stm32-simple-sources/SKILL.md) — Подключение исходников к проекту
- [stm32-module-creator](../stm32-module-creator/SKILL.md) — Создание модулей-библиотек
```
