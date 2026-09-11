#pragma once
#include <stdint.h>

/**
 * @brief Проверка, запущен ли код в эмуляторе QEMU.
 */
bool Bsp_IsRunningInQemu();

/**
 * @brief Экстренное завершение работы (для CI/CD).
 */
void Bsp_QemuExit( int code );

/**
 * @brief Вывод стартовой диагностической информации в UART.
 */
void Bsp_PrintFirmwareInfo();

/**
 * @brief Проверка стабильности питания перед запуском периферии.
 * @return true, если питание VDD в норме.
 */
bool Bsp_CheckPowerSupply();