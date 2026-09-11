#include <Arduino.h>
#include "Config.h"
#include "Bsp.h"
// #include "FsmManager.h"
// #include "PumpDriver.h"

// Глобальные объекты бизнес-логики
// PumpDriver pump;
// FsmManager fsm(&pump);

/**
 * @brief Функция выполняется до вызова конструкторов глобальных C++ объектов.
 */
__attribute__( ( constructor( 101 ) ) ) void premain()
{
    init(); // Инициализация HAL и SysTick (Arduino Core)
}

void setup()
{
    if ( !Bsp_IsRunningInQemu() )
    {
        Serial.begin( Config::SERIAL_BAUD );
    }

    Bsp_PrintFirmwareInfo();

    bool powerOk = Bsp_CheckPowerSupply();

    // Инициализация модулей
    // pump.init();
    // fsm.init();

    if ( !powerOk )
    {
        // fsm.setHardwareFault( true );
        sys_log("Setup: Power fault detected. System locked in SAFE mode.\n");
    }
    
    sys_log("Setup complete.\n");
}

void loop()
{
    // 1. Асинхронная периферия (работает без задержек)
    // IWatchdog.reload();
    // networkTick();

    // 2. Бизнес-логика (выполняется строго с периодом 1 мс)
    static uint32_t lastLogicMs = 0;
    uint32_t now = millis();

    if ( now - lastLogicMs >= 1 )
    {
        lastLogicMs = now;

        // Опрос аппаратуры
        // pump.updateTelemetry();
        
        // Обновление конечного автомата
        // fsm.update();
        
        // Индикация HMI
        // bool hasError = fsm.hasSystemErrors();
        // updateLeds(hasError);
    }
}

/**
 * @brief Точка входа C++ (заменяет main из ядра Arduino)
 */
int main( void )
{
    initVariant(); // Специфика платы STM32duino
    setup();

    while ( 1 )
    {
        loop();
        serialEventRun();
    }
}