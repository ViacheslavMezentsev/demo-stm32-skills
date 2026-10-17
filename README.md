# demo-stm32-skills

Набор навыков (Agent Skills) для ИИ-ассистентов, помогающих в разработке под STM32 и совместимые микроконтроллеры: настройка сборки, генерация кода, архитектура прошивок, отладка и диагностика.

Все навыки оформлены по [спецификации Agent Skills](https://agentskills.io/specification) и подходят для Claude Code, Cursor, Codex и других агентов, поддерживающих этот формат.

Репозиторий демонстрационный: часть навыков привязана к фреймворку [stm32-cmake-yml](https://github.com/ViacheslavMezentsev/stm32-cmake-yml) и может потребовать адаптации под ваш проект.

## Навыки

### Сборка и структура проекта

| Навык | Назначение |
|-------|------------|
| [stm32-config-manager](stm32-config-manager/) | Настройка проекта через `stm32_config.yml`: MCU, HAL/CMSIS, FreeRTOS, линковка, CRC |
| [stm32-module-creator](stm32-module-creator/) | Создание библиотек-модулей с наследованием флагов компиляции |
| [stm32-simple-sources](stm32-simple-sources/) | Подключение исходников (код CubeMX, системные файлы) напрямую к основной цели |
| [stm32-build-helper](stm32-build-helper/) | Разбор ошибок CMake, GCC и линкера |
| [stm32-cubemx-codegen](stm32-cubemx-codegen/) | Генерация проекта из `.ioc` через STM32CubeMX в режиме командной строки |
| [stm32-vscode-cmake-scaffold](stm32-vscode-cmake-scaffold/) | Окружение VS Code + CMake + Ninja для проекта CubeMX |
| [demo-stm32-cmake-example-builder](demo-stm32-cmake-example-builder/) | Нумерованные примеры для репозитория demo-stm32-cmake |
| [stm32-arduino-cmake-architecture](stm32-arduino-cmake-architecture/) | Прошивки на Arduino Core со сборкой через CMake |

### Архитектура и код

| Навык | Назначение |
|-------|------------|
| [stm32-hard-realtime-architecture](stm32-hard-realtime-architecture/) | Паттерны Hard Real-Time на C++17 и ETL |
| [stm32-bsp-driver-pattern](stm32-bsp-driver-pattern/) | Драйверы внешних микросхем в стиле BSP от ST |
| [stm32-acoustic-modem-morse](stm32-acoustic-modem-morse/) | Передача диагностики через зуммер (OOK Morse) и декодер на Python |
| [cpp-clang-format](cpp-clang-format/) | Единый стиль кода C/C++ через `.clang-format` |

### Отладка и диагностика

| Навык | Назначение |
|-------|------------|
| [stm32-rtt-console](stm32-rtt-console/) | Консоль RTT через OpenOCD и ST-Link без занятия UART |
| [stm32-swo-trace](stm32-swo-trace/) | Трассировка SWO/ITM и профилирование по DWT |
| [stm32-ros-alt-channel](stm32-ros-alt-channel/) | Вывод переменных и регистров в ROS1 по SWD или rosserial |
| [stm32-renode-emulator](stm32-renode-emulator/) | Проверка прошивки в эмуляторе Renode без платы |
| [weact-logic-analyzer](weact-logic-analyzer/) | Измерения логическим анализатором WeAct через sigrok-cli |

### Документация и процесс

| Навык | Назначение |
|-------|------------|
| [embedded-tech-spec](embedded-tech-spec/) | Техническое задание на модуль прошивки с требованиями и тест-кейсами |
| [ap-patch-generation](ap-patch-generation/) | Патчи в формате AP для чатов без доступа к файловой системе |

## Установка

Скопируйте нужные папки навыков туда, где их ищет ваш агент. Для Claude Code это `.claude/skills/` в проекте или `~/.claude/skills/` для всех проектов:

```bash
git clone https://github.com/ViacheslavMezentsev/demo-stm32-skills.git
cp -r demo-stm32-skills/stm32-config-manager your-project/.claude/skills/
```

Либо подключите репозиторий целиком как подмодуль:

```bash
git submodule add https://github.com/ViacheslavMezentsev/demo-stm32-skills.git .claude/skills/stm32
```

## Формат навыка

Каждый навык — папка с файлом `SKILL.md` и, при необходимости, подпапками `scripts/`, `references/` и `assets/`. Заголовок `SKILL.md` во всех навыках репозитория единообразен:

```yaml
---
name: stm32-example            # совпадает с именем папки; a-z, 0-9 и дефисы, до 64 символов
description: Что делает навык и когда его использовать (до 1024 символов).
license: MIT
compatibility: Требуемые инструменты и окружение (необязательно, до 500 символов).
metadata:
  author: Viacheslav Mezentsev
  version: "1.0"
allowed-tools: Bash Read Write Edit   # необязательно
---
```

Дополнительные сведения (версия, теги, ссылки) помещаются в `metadata` как строки, а не в отдельные поля верхнего уровня. Объём `SKILL.md` желательно держать в пределах 500 строк, вынося подробности в `references/`.

Проверить навык на соответствие спецификации можно утилитой [skills-ref](https://github.com/agentskills/agentskills/tree/main/skills-ref):

```bash
skills-ref validate ./stm32-config-manager
```

## Ограничения

- Навыки сборки рассчитаны на `stm32-cmake-yml` 0.5+ и `arm-none-eabi-gcc`.
- Навыки отладки проверялись с OpenOCD и ST-Link; другие отладчики могут потребовать адаптации.
- Из RTOS поддерживается FreeRTOS.

## Участие

Исправления и новые навыки принимаются через pull request из fork. Правила — в [AGENTS.md](AGENTS.md):
коммиты в формате Conventional Commits на английском, подписанные ключом, зарегистрированным в GitHub;
навык должен проходить `skills-ref validate` (то же проверяет CI). Изменения отмечайте в разделе
Unreleased файла [CHANGELOG.md](CHANGELOG.md).

## Лицензия

[MIT](LICENSE)
