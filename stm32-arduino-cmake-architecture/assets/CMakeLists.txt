cmake_minimum_required(VERSION 3.16)

set(CMAKE_C_STANDARD 17)
set(CMAKE_CXX_STANDARD 17)

# Путь к вашему toolchain-файлу для ARM GCC
set(CMAKE_TOOLCHAIN_FILE "${CMAKE_CURRENT_SOURCE_DIR}/cmake/gcc-arm-none-eabi.cmake")

project(stm32_plc_firmware C CXX ASM)

# Линкер-скрипт MCU
set(LINKER_FILE "${CMAKE_CURRENT_SOURCE_DIR}/STM32G431XX_FLASH.ld")

add_library(ArduinoDefinitions INTERFACE)
add_library(Arduino::Definitions ALIAS ArduinoDefinitions)

# Общие макросы для Arduino Core и HAL
target_compile_definitions(ArduinoDefinitions INTERFACE
    STM32G4xx
    STM32G431xx
    ARDUINO=10607
    ARDUINO_ARCH_STM32
    USE_HAL_DRIVER
    USE_FULL_LL_DRIVER
    HSE_VALUE=8000000UL
    SERIAL_TX_BUFFER_SIZE=512
    $<$<CONFIG:DEBUG>:DEBUG_>
)

# Оптимизации и флаги (Hard Real-Time)
target_compile_options(ArduinoDefinitions INTERFACE
    -mthumb -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16
    -mlittle-endian -fcommon
    -fdata-sections -ffunction-sections
    $<$<COMPILE_LANGUAGE:CXX>:
        -fpermissive -fno-threadsafe-statics -fno-exceptions -fno-rtti>
    -Wall
    -flto=auto
    $<$<CONFIG:DEBUG>:-O2 -g3>
    $<$<CONFIG:RELEASE>:-O2 -DNDEBUG>
)

# Отключаем встроенный main() от Arduino
set(USE_CORE_MAIN OFF CACHE BOOL "" FORCE)

# Подключение модулей ядра (пути зависят от расположения Arduino Core STM32)
add_subdirectory(Arduino/Core)
add_subdirectory(Arduino/libraries/SrcWrapper)

# Исполняемый файл проекта
add_executable(${PROJECT_NAME}
    src/main.cpp
    src/Bsp.cpp
    src/FsmManager.cpp
)

target_link_libraries(${PROJECT_NAME} PRIVATE
    Arduino::Core
    Arduino::SrcWrapper
    Arduino::Definitions
)

target_include_directories(${PROJECT_NAME} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src/include
    ${CMAKE_CURRENT_SOURCE_DIR}/modules/etl/include
)

target_link_options(${PROJECT_NAME} PRIVATE
    -mthumb -mcpu=cortex-m4 -mfloat-abi=hard -mfpu=fpv4-sp-d16 -mlittle-endian
    --specs=nano.specs
    -flto=auto
    -T${LINKER_FILE}
    LINKER:-Map=${PROJECT_NAME}.map,--gc-sections,--print-memory-usage
)