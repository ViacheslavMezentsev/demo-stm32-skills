# Использование драйвера и сценарии

Примеры вызова драйвера из приложения и типовые сценарии: новый драйвер, рефакторинг, обёртка над готовым кодом.

## Использование в приложении

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

## Использование через структуру драйвера

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

## Сценарий 1: Новый драйвер по datasheet

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

## Сценарий 2: Рефакторинг существующего кода

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

## Сценарий 3: Обёртка для существующего кода

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
