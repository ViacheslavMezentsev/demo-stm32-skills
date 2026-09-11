# Шаблоны файлов .vscode/

Все файлы кроме `settings.json` одинаковы для всех примеров.

## extensions.json

```json
{
    "recommendations": [
        "ms-vscode.cpptools",
        "ms-vscode.cmake-tools",
        "marus25.cortex-debug"
    ]
}
```

## launch.json

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Debug (gdb/stlink)",
      "cwd": "${workspaceRoot}",
      "executable": "${config:executable}",
      "interface": "swd",
      "request": "launch",
      "type": "cortex-debug",
      "servertype": "stlink",
      "device": "${config:device}",
      "preLaunchTask": "CMake: build",
      "preRestartCommands": ["load", "enable breakpoint", "monitor reset"],
      "runToEntryPoint": "main",
      "svdFile": "${config:svdFile}",
      "liveWatch": { "enabled": false, "samplesPerSecond": 4 }
    },
    {
      "name": "Debug (gdb/jlink)",
      "cwd": "${workspaceRoot}",
      "executable": "${config:executable}",
      "interface": "swd",
      "request": "launch",
      "type": "cortex-debug",
      "servertype": "jlink",
      "device": "${config:device}",
      "preLaunchTask": "CMake: build",
      "preRestartCommands": ["load", "enable breakpoint", "monitor reset"],
      "runToEntryPoint": "main",
      "svdFile": "${config:svdFile}",
      "liveWatch": { "enabled": false, "samplesPerSecond": 4 },
      "rttConfig": {
        "enabled": false,
        "address": "auto",
        "decoders": [{ "label": "RTT channel 0", "port": 0, "timestamp": true, "type": "console" }]
      }
    },
    {
      "name": "Debug (pyocd)",
      "cwd": "${workspaceRoot}",
      "executable": "${config:executable}",
      "interface": "swd",
      "request": "launch",
      "type": "cortex-debug",
      "servertype": "pyocd",
      "targetId": "${config:device}",
      "preLaunchTask": "CMake: build",
      "preRestartCommands": ["load", "enable breakpoint", "monitor reset"],
      "runToEntryPoint": "main",
      "svdFile": "${config:svdFile}",
      "liveWatch": { "enabled": false, "samplesPerSecond": 4 }
    },
    {
      "name": "Debug (ocd/stlink)",
      "cwd": "${workspaceRoot}",
      "executable": "${config:executable}",
      "interface": "swd",
      "request": "launch",
      "type": "cortex-debug",
      "servertype": "openocd",
      "device": "${config:device}",
      "preLaunchTask": "CMake: build",
      "preRestartCommands": ["load", "enable breakpoint", "monitor reset"],
      "runToEntryPoint": "main",
      "svdFile": "${config:svdFile}",
      "liveWatch": { "enabled": false, "samplesPerSecond": 4 },
      "configFiles": ["interface/stlink.cfg", "target/${config:targetFamily}.cfg"]
    },
    {
      "name": "Debug (ocd/jlink)",
      "cwd": "${workspaceRoot}",
      "executable": "${config:executable}",
      "interface": "swd",
      "request": "launch",
      "type": "cortex-debug",
      "servertype": "openocd",
      "device": "${config:device}",
      "preLaunchTask": "CMake: build",
      "preRestartCommands": ["load", "enable breakpoint", "monitor reset"],
      "runToEntryPoint": "main",
      "svdFile": "${config:svdFile}",
      "liveWatch": { "enabled": false, "samplesPerSecond": 4 },
      "configFiles": ["interface/jlink.cfg", "target/${config:targetFamily}.cfg"]
    },
    {
      "name": "Debug (ocd/cmsis-dap)",
      "cwd": "${workspaceRoot}",
      "executable": "${config:executable}",
      "interface": "swd",
      "request": "launch",
      "type": "cortex-debug",
      "servertype": "openocd",
      "device": "${config:device}",
      "preLaunchTask": "CMake: build",
      "preRestartCommands": ["load", "enable breakpoint", "monitor reset"],
      "runToEntryPoint": "main",
      "svdFile": "${config:svdFile}",
      "liveWatch": { "enabled": false, "samplesPerSecond": 4 },
      "configFiles": ["interface/cmsis-dap.cfg", "target/${config:targetFamily}.cfg"]
    },
    {
      "name": "Debug (stutil)",
      "cwd": "${workspaceRoot}",
      "executable": "${config:executable}",
      "interface": "swd",
      "request": "launch",
      "type": "cortex-debug",
      "servertype": "stutil",
      "device": "${config:device}",
      "preLaunchTask": "CMake: build",
      "preRestartCommands": ["load", "enable breakpoint", "monitor reset"],
      "runToEntryPoint": "main",
      "svdFile": "${config:svdFile}",
      "liveWatch": { "enabled": false, "samplesPerSecond": 4 }
    },
    {
      "name": "Debug (qemu)",
      "cwd": "${workspaceFolder}",
      "executable": "${config:executable}",
      "request": "launch",
      "type": "cortex-debug",
      "servertype": "external",
      "gdbTarget": "localhost:1234",
      "device": "${config:device}",
      "runToEntryPoint": "main",
      "svdFile": "${config:svdFile}",
      "preLaunchTask": "Запустить qemu-arm",
      "liveWatch": { "enabled": false, "samplesPerSecond": 2 }
    }
  ]
}
```

## tasks.json

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "type": "cmake",
      "label": "CMake: build",
      "command": "build",
      "targets": ["${fileWorkspaceFolderBasename}"],
      "problemMatcher": [],
      "group": "build"
    },
    {
      "label": "Запустить qemu-arm",
      "type": "shell",
      "command": "qemu-system-arm",
      "args": [
        "-M", "${input:qemuMachine}",
        "-nographic", "-gdb", "tcp::1234", "-S",
        "-kernel", "${command:cmake.buildDirectory}/${workspaceFolderBasename}.elf",
        "-semihosting"
      ],
      "isBackground": true,
      "problemMatcher": {
        "pattern": [{ "regexp": ".", "file": 1, "location": 2, "message": 3 }],
        "background": { "activeOnStart": true, "beginsPattern": "^.*", "endsPattern": "^$" }
      }
    },
    {
      "label": "Сбросить (j-link)", "type": "shell", "command": "cmd",
      "args": ["/C", "((echo device ${config:device} & echo si SWD & echo speed 4000 & echo r & echo h & echo q) > flash.jlink) && (JLink.exe -nogui 1 -CommandFile flash.jlink) 1> nul && echo \"Успешно\" || echo \"Ошибка\""],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    },
    {
      "label": "Очистить всё (j-link)", "type": "shell", "command": "cmd",
      "args": ["/C", "((echo r & echo h & echo erase & echo q) > flash.jlink) && (JLink.exe -device ${config:device} -if swd -speed 4000 -nogui 1 -CommandFile flash.jlink) 1> nul && echo \"Успешно\" || echo \"Ошибка\""],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    },
    {
      "label": "Прошить (j-link)", "type": "shell", "command": "cmd",
      "args": ["/C", "((echo r & echo h & echo loadfile ${command:cmake.buildType}/${workspaceFolderBasename}.hex & echo q) > flash.jlink) && (JLink.exe -device ${config:device} -if swd -speed 4000 -nogui 1 -CommandFile flash.jlink) 1> nul && echo \"Успешно\" || echo \"Ошибка\""],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    },
    {
      "label": "Сброс (pyocd)", "type": "shell", "command": "cmd",
      "args": ["/C", "pyocd reset -t ${config:device} -m sw"],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    },
    {
      "label": "Очистить всё (pyocd)", "type": "shell", "command": "cmd",
      "args": ["/C", "pyocd erase -t ${config:device} --mass"],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    },
    {
      "label": "Прошить (pyocd)", "type": "shell", "command": "cmd",
      "args": ["/C", "pyocd flash -t ${config:device} ${command:cmake.buildType}/${workspaceFolderBasename}.hex"],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    },
    {
      "label": "Сброс (st-flash)", "type": "shell", "command": "cmd",
      "args": ["/C", "st-flash reset"],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    },
    {
      "label": "Очистить всё (st-flash)", "type": "shell", "command": "cmd",
      "args": ["/C", "st-flash erase"],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    },
    {
      "label": "Прошить (st-flash)", "type": "shell", "command": "cmd",
      "args": ["/C", "st-flash --reset --format \"ihex\" --freq=4000k write ${command:cmake.buildType}/${workspaceFolderBasename}.hex"],
      "options": { "cwd": "${workspaceFolder}/build" }, "group": "build"
    }
  ],
  "inputs": [
    {
      "id": "qemuMachine",
      "type": "pickString",
      "description": "Выберите машину для эмуляции в QEMU",
      "options": [
        { "label": "Netduino 2 (STM32F205RF, Cortex-M3)", "value": "netduino2" },
        { "label": "Netduino Plus 2 (STM32F405RG, Cortex-M4)", "value": "netduinoplus2" }
      ],
      "default": "netduino2"
    }
  ]
}
```

## c_cpp_properties.json

Изменяемое поле: `<FAMILY_DEFINE>` (например `STM32F411xx`).

```json
{
    "env": {
        "defaultIncludePath": [
            "${config:toolchain}/arm-none-eabi/include",
            "${config:toolchain}/arm-none-eabi/include/c++/**"
        ],
        "defaultDefines": ["__GNUC__", "USE_HAL_DRIVER", "<FAMILY_DEFINE>"],
        "compiler": ""
    },
    "configurations": [
        {
            "name": "Debug",
            "includePath": ["${defaultIncludePath}"],
            "defines": ["${defaultDefines}", "DEBUG"],
            "intelliSenseMode": "gcc-arm",
            "compilerPath": "${compiler}",
            "compileCommands": "${workspaceFolder}/build/Debug/compile_commands.json",
            "configurationProvider": "ms-vscode.cmake-tools",
            "cStandard": "gnu17",
            "cppStandard": "gnu++17",
            "browse": {
                "limitSymbolsToIncludedHeaders": true,
                "databaseFilename": "${workspaceFolder}/.vscode/browse.vc.db"
            }
        },
        {
            "name": "Release",
            "includePath": ["${defaultIncludePath}"],
            "defines": ["${defaultDefines}"],
            "intelliSenseMode": "gcc-arm",
            "compilerPath": "${compiler}",
            "compileCommands": "${workspaceFolder}/build/Release/compile_commands.json",
            "configurationProvider": "ms-vscode.cmake-tools",
            "cStandard": "gnu17",
            "cppStandard": "gnu++17",
            "browse": {
                "limitSymbolsToIncludedHeaders": true,
                "databaseFilename": "${workspaceFolder}/.vscode/browse.vc.db"
            }
        }
    ],
    "version": 4
}
```

## settings.json

Специфичен для каждого проекта. Подставить `<MCU_DEVICE>`, `<SVD_FILE>`, `<TARGET_FAMILY>`.

```json
{
    "cSpell.ignoreRegExpList": ["\\b[0-9A-Z_]+\\b"],
    "cmake.generator": "Ninja",
    "cmake.buildDirectory": "${workspaceRoot}/build/${buildType}",
    "cmake.configureEnvironment": {
        "CMAKE_EXPORT_COMPILE_COMMANDS": "on",
        "CMAKE_USER_HOME": "${userHome}"
    },
    "cortex-debug.variableUseNaturalFormat": false,
    "cortex-debug.pyocdPath.windows": "pyocd",
    "cortex-debug.gdbPath.windows": "${config:toolchain}/bin/arm-none-eabi-gdb.exe",
    "cortex-debug.stutilPath.windows": "st-util.exe",
    "cortex-debug.stlinkPath.windows": "ST-LINK_gdbserver.exe",
    "cortex-debug.JLinkGDBServerPath.windows": "JLinkGDBServerCL.exe",
    "cortex-debug.openocdPath.windows": "openocd.exe",
    "cortex-debug.gdbPath.linux": "${config:toolchain}/bin/arm-none-eabi-gdb",
    "cortex-debug.pyocdPath.linux": "pyocd",
    "cortex-debug.stutilPath.linux": "st-util",
    "cortex-debug.JLinkGDBServerPath.linux": "JLinkGDBServerCLExe",
    "cortex-debug.openocdPath.linux": "openocd",
    "executable": "${command:cmake.buildDirectory}/${workspaceFolderBasename}.elf",
    "device": "<MCU_DEVICE>",
    "svdFile": "${workspaceRoot}/<SVD_FILE>",
    "targetFamily": "<TARGET_FAMILY>",
    "toolchain": "${env:USERPROFILE}/xpack-arm-none-eabi-gcc-13.3.1-1.1"
    //"toolchain": "${env:HOME}/xpack-arm-none-eabi-gcc-13.3.1-1.1"
}
```
