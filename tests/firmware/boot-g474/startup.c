/* Vector table and reset handler for the CI firmware (Cortex-M4, no CMSIS). */
#include <stdint.h>

extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;
int main( void );

void Reset_Handler( void )
{
    uint32_t *src = &_sidata;
    for ( uint32_t *dst = &_sdata; dst < &_edata; )
    {
        *dst++ = *src++;
    }
    for ( uint32_t *dst = &_sbss; dst < &_ebss; )
    {
        *dst++ = 0u;
    }
    main();
    for ( ;; )
    {
    }
}

void Default_Handler( void )
{
    for ( ;; )
    {
    }
}

__attribute__( ( section( ".isr_vector" ), used ) ) void ( *const vectors[ 16 ] )( void ) = {
    (void ( * )( void )) & _estack, Reset_Handler, Default_Handler, Default_Handler, Default_Handler,
    Default_Handler, Default_Handler, 0, 0, 0, 0, Default_Handler, Default_Handler, 0, Default_Handler,
    Default_Handler,
};
