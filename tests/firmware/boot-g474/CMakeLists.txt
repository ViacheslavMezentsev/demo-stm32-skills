# CI firmware for the emulator checks. The toolchain file comes from the
# stm32-vscode-cmake-scaffold skill, so this build also checks that asset:
#   cmake -S tests/firmware/boot-g474 -B build/boot-g474 -G Ninja \
#     -DCMAKE_TOOLCHAIN_FILE=stm32-vscode-cmake-scaffold/assets/gcc-arm-none-eabi.cmake \
#     -DSTM32_TOOLCHAIN_PATH=<xpack-arm-none-eabi-gcc>
cmake_minimum_required(VERSION 3.20)
project(boot_g474 C)

add_executable(firmware boot.c startup.c)
target_compile_options(firmware PRIVATE
    -mcpu=cortex-m4 -mthumb -Os -g -ffreestanding -fno-builtin -Wall -Wextra -Werror)
target_link_options(firmware PRIVATE
    -mcpu=cortex-m4 -mthumb -nostdlib -Wl,--gc-sections -Wl,-Map=firmware.map
    -T${CMAKE_CURRENT_SOURCE_DIR}/boot.ld)
stm32_print_size_of_target(firmware)
