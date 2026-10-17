# Реализация драйвера ({device}.c)

Экземпляр структуры драйвера и типовые функции: инициализация и чтение данных.

## Экземпляр структуры драйвера

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

## Пример функции инициализации

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

## Пример функции чтения данных

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
