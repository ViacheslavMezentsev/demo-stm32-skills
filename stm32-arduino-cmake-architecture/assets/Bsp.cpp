#include "Bsp.h"
#include "Config.h"
#include <Arduino.h>
#include <stdarg.h>
#include <stdio.h>

extern "C" void HardFault_Handler( void );

// ============================================================================
// СИСТЕМНЫЙ ЛОГГЕР
// ============================================================================
void _sys_log_impl( const char *format, ... )
{
    char buf[256];
    
    // Метка времени в формате [  сек.мск]
    uint32_t tick    = millis();
    uint32_t seconds = tick / 1000;
    uint32_t ms      = tick % 1000;
    int len = snprintf( buf, sizeof( buf ), "[%5lu.%03lu] ", seconds, ms );

    va_list args;
    va_start( args, format );
    vsnprintf( buf + len, sizeof( buf ) - len, format, args );
    va_end( args );

    if ( Bsp_IsRunningInQemu() )
    {
        // Использование ARM Semihosting (SYS_WRITE0) для QEMU
        register int r0 __asm__( "r0" ) = 0x04;
        register const char *r1 __asm__( "r1" ) = buf;
        __asm__ volatile( "bkpt 0xAB" : "+r"( r0 ) : "r"( r1 ) : "memory" );
    }
    else
    {
        // Асинхронная отправка в UART (работает через прерывания)
        Serial.print( buf );
    }
}

// ============================================================================
// ПРИМЕР: HARD FAULT HANDLER
// ============================================================================
extern "C" void HardFault_Handler( void )
{
    volatile uint32_t* sp = ( volatile uint32_t* ) __get_MSP();

    sys_log( "!!! HARD FAULT: CFSR=0x%08lX PC=0x%08lX LR=0x%08lX\n", 
             ( uint32_t ) SCB->CFSR, sp[6], sp[5] );

    Serial.flush();
    __disable_irq();

    while ( 1 )
    {
        digitalWrite( Config::PIN_LED_STATUS, !digitalRead( Config::PIN_LED_STATUS ) );
        for ( volatile int i = 0; i < 2000000; i++ ) {}
    }
}

// Заглушки для примера
bool Bsp_IsRunningInQemu() { return false; }
void Bsp_PrintFirmwareInfo() { sys_log("System Booting...\n"); }
bool Bsp_CheckPowerSupply() { return true; }
void Bsp_QemuExit( int code ) { while(1); }