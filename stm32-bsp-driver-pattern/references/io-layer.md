# Уровень ввода-вывода ({device}_io.c/h)

Единственная часть драйвера, зависящая от платы: заголовок IO-уровня и реализации для I2C и SPI.

**Это самый важный файл для портируемости!** Здесь реализуется конкретный способ подключения (I2C, SPI, UART).

## Заголовок IO-уровня

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

## Реализация для I2C

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

## Реализация для SPI

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
