#pragma once

#include <Arduino.h>

// Внутренняя функция логирования BSP (не вызывать напрямую!)
void _sys_log_impl( const char *format, ... );

namespace Config
{
    static constexpr bool DebugLogsEnabled = true;

    // ПИНЫ ПЛК
    constexpr uint8_t PIN_SENSOR_ANALOG = PA0;
    constexpr uint8_t PIN_PUMP_RELAY    = PA1;
    constexpr uint8_t PIN_LED_STATUS    = PC13;

    // СИСТЕМНЫЕ КОНСТАНТЫ
    constexpr uint32_t SERIAL_BAUD      = 115200;
    
    // ТАЙМИНГИ И ЛОГИКА
    constexpr uint32_t MAX_PRESSURE_VAL = 3000; ///< Порог аварийного давления
    constexpr uint32_t PUMP_MAX_RUN_MS  = 5000; ///< Защита от перегрева насоса
}

// Макрос-обертка для вырезания логов в Release-сборке (Zero-overhead).
#define sys_log(...) do { if constexpr (Config::DebugLogsEnabled) { _sys_log_impl(__VA_ARGS__); } } while(0)