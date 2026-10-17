# Заголовок драйвера ({device}.h)

Шаблон заголовка драйвера в стиле BSP: элементы в порядке их следования в файле. Пример — гироскоп L3GD20.

## Стандартные элементы

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

## Карта регистров (Register Mapping)

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

## Конфигурационные определения

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

## Структура инициализации

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

## Структура драйвера (Driver Structure)

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

## Прототипы функций

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

## Завершение заголовка

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
