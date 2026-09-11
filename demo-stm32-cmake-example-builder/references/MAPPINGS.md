# Справочные таблицы маппинга

## Семейства MCU

| Семейство  | `targetFamily` | Папка SVD  | FreeRTOS-порт |
|------------|----------------|------------|---------------|
| STM32F0xx  | `stm32f0x`     | `stm32f0`  | `ARM_CM0`     |
| STM32F1xx  | `stm32f1x`     | `stm32f1`  | `ARM_CM3`     |
| STM32F4xx  | `stm32f4x`     | `stm32f4`  | `ARM_CM4F`    |
| STM32F7xx  | `stm32f7x`     | `stm32f7`  | `ARM_CM7`     |
| STM32H5xx  | `stm32h5x`     | `stm32h5`  | `ARM_CM33`    |
| STM32H7xx  | `stm32h7x`     | `stm32h7`  | `ARM_CM7`     |

## Startup-файлы

| MCU          | startup-файл                  |
|--------------|-------------------------------|
| STM32F411xE  | `startup_stm32f411xe.s`       |
| STM32F103xB  | `startup_stm32f103xb.s`       |
| STM32F407xG  | `startup_stm32f407xx.s`       |
| STM32H743xx  | `startup_stm32h743xx.s`       |

Паттерн: `startup_stm32<chip_lower>x<flash_density>.s`.
Уточнять по содержимому `Core/Startup/` сгенерированного проекта.

## IP-блоки → `hal_components`

Базовые (всегда): `CORTEX`, `FLASH`, `RCC`, `GPIO`, `PWR`

| IP в `.ioc`         | HAL-компонент |
|---------------------|---------------|
| `TIM1` / `TIM2` / … | `TIM`         |
| `USART1` / `UART2`  | `UART`        |
| `I2C1` / …          | `I2C`         |
| `SPI1` / …          | `SPI`         |
| `ADC1` / …          | `ADC`         |
| `DAC`               | `DAC`         |
| `USB_OTG_FS`        | `PCD`         |
| `CAN1`              | `CAN`         |
| `RTC`               | `RTC`         |
| `DMA`               | `DMA`         |
| `CRC`               | `CRC`         |
