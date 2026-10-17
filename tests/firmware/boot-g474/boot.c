/*
 * Minimal CI firmware for the skills' emulator checks (no HAL, no libc).
 *
 * Prints a timestamped boot log over ARM semihosting, the same way a project's
 * sys_log does, so that stm32-renode-emulator/scripts/check_startup_log.py can
 * verify it. Memory map fits STM32G474 (Renode mock) and the QEMU netduinoplus2
 * machine (STM32F405): Flash at 0x08000000, SRAM at 0x20000000.
 */
#include <stdint.h>

#define SCB_CPUID (*(volatile const uint32_t *)0xE000ED00u)
#define TELEMETRY_LOOPS 50000000u

typedef struct
{
    uint8_t major;
    uint8_t minor;
    uint8_t patch;
    uint8_t reserved;
    uint16_t build;
    uint16_t revision;
} FwVersion;

/* Read by check_startup_log.py ({version}, {build}, {revision}). */
__attribute__( ( section( ".fw_version" ), used ) ) const FwVersion fw_version = { 1, 2, 3, 0, 45, 6 };

/* Telemetry variables: stm32-ros-alt-channel resolves their addresses from the ELF. */
volatile uint32_t telemetry_counter;
volatile uint32_t telemetry_state = 0xA5u;

static uint32_t ticks;

static void semihost_write0( const char *text )
{
    register uint32_t r0 __asm__( "r0" ) = 0x04u; /* SYS_WRITE0 */
    register const char *r1 __asm__( "r1" ) = text;
    __asm__ volatile( "bkpt 0xAB" : "+r"( r0 ) : "r"( r1 ) : "memory" );
}

/* SYS_EXIT with ADP_Stopped_ApplicationExit: QEMU terminates, Renode halts the core. */
static void semihost_exit( void )
{
    register uint32_t r0 __asm__( "r0" ) = 0x18u;
    register uint32_t r1 __asm__( "r1" ) = 0x20026u;
    __asm__ volatile( "bkpt 0xAB" : "+r"( r0 ) : "r"( r1 ) : "memory" );
}

static char *put_str( char *out, const char *text )
{
    while ( *text )
    {
        *out++ = *text++;
    }
    return out;
}

static char *put_dec( char *out, uint32_t value, int width )
{
    char digits[ 10 ];
    int count = 0;
    do
    {
        digits[ count++ ] = (char)( '0' + value % 10u );
        value /= 10u;
    } while ( value && count < 10 );
    while ( width-- > count )
    {
        *out++ = '0';
    }
    while ( count )
    {
        *out++ = digits[ --count ];
    }
    return out;
}

static char *put_hex( char *out, uint32_t value )
{
    out = put_str( out, "0x" );
    for ( int shift = 28; shift >= 0; shift -= 4 )
    {
        *out++ = "0123456789ABCDEF"[ ( value >> shift ) & 0xFu ];
    }
    return out;
}

/* One log line: "[   0.001] <text>\n" with a fake millisecond clock. */
static void log_line( const char *text, const char *tail_text, uint32_t tail_value, int tail_kind )
{
    char line[ 96 ];
    char *out = line;
    uint32_t ms = ticks++;
    out = put_str( out, "[" );
    uint32_t seconds = ms / 1000u;
    out = put_str( out, seconds < 10u ? "   " : "  " );
    out = put_dec( out, seconds, 1 );
    out = put_str( out, "." );
    out = put_dec( out, ms % 1000u, 3 );
    out = put_str( out, "] " );
    out = put_str( out, text );
    if ( tail_kind == 1 )
    {
        out = put_dec( out, tail_value, 1 );
    }
    else if ( tail_kind == 2 )
    {
        out = put_hex( out, tail_value );
    }
    if ( tail_text )
    {
        out = put_str( out, tail_text );
    }
    *out++ = '\n';
    *out = '\0';
    semihost_write0( line );
}

static const char *core_name( uint32_t cpuid )
{
    switch ( ( cpuid >> 4 ) & 0xFFFu )
    {
    case 0xC23u:
        return "Cortex-M3";
    case 0xC24u:
        return "Cortex-M4";
    case 0xC27u:
        return "Cortex-M7";
    default:
        return "Cortex-M?";
    }
}

int main( void )
{
    uint32_t cpuid = SCB_CPUID;
    char text[ 64 ];
    char *out;

    log_line( "boot: start", 0, 0, 0 );

    out = put_str( text, "CPU: " );
    out = put_str( out, core_name( cpuid ) );
    out = put_str( out, " (CPUID " );
    *out = '\0';
    log_line( text, ")", cpuid, 2 );

    out = put_str( text, "Firmware: v" );
    out = put_dec( out, fw_version.major, 1 );
    out = put_str( out, "." );
    out = put_dec( out, fw_version.minor, 1 );
    out = put_str( out, "." );
    out = put_dec( out, fw_version.patch, 1 );
    out = put_str( out, " build " );
    out = put_dec( out, fw_version.build, 1 );
    out = put_str( out, "." );
    *out = '\0';
    log_line( text, 0, fw_version.revision, 1 );

    log_line( "Telemetry state: ", 0, telemetry_state, 2 );
    log_line( "boot: done", 0, 0, 0 );

    /* A RAM variable that changes while the core runs: about 2 s of emulated time in
       Renode, well under a second in QEMU. The debugger-side checks read it twice. */
    while ( telemetry_counter < TELEMETRY_LOOPS )
    {
        telemetry_counter++;
    }
    semihost_exit();
    for ( ;; )
    {
    }
}
