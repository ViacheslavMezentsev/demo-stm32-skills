# Changelog

Все заметные изменения в этом проекте документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [Unreleased]

### Добавлено

- Правила работы для агентов и участников: `AGENTS.md`, `CLAUDE.md` (подключает `AGENTS.md`).
- Памятки `TODO.md`, `CHANGELOG.md`, `HOWTO.md`.
- Проверка навыков в CI (`.github/workflows/skills.yml`): `skills-ref validate` для каждого навыка
  и предупреждение, если SKILL.md длиннее 500 строк.
- `.gitignore` (кэши Python, прошивки, логи, резервные копии) и `.gitattributes` (концы строк).

### Изменено

- Заголовки всех 19 навыков приведены к спецификации Agent Skills: единый порядок полей,
  поля `version` и `tags` перенесены в `metadata`, значения `metadata` — строки, добавлен `author`.
- Лицензия всех навыков — MIT, как в `LICENSE`: в stm32-renode-emulator, stm32-ros-alt-channel,
  stm32-rtt-console, stm32-swo-trace и weact-logic-analyzer была указана BSD-3-Clause,
  в ap-patch-generation, embedded-tech-spec и stm32-vscode-cmake-scaffold лицензия отсутствовала.
- В описания восьми навыков добавлено, когда их применять: stm32-acoustic-modem-morse,
  stm32-arduino-cmake-architecture, stm32-hard-realtime-architecture, stm32-build-helper,
  stm32-module-creator, stm32-rtt-console, stm32-swo-trace, stm32-vscode-cmake-scaffold.
- README переписан: перечислены все навыки по группам, описаны установка, формат навыка и проверка,
  убраны эмодзи.
- Из тел навыков убраны эмодзи; пометка ⚠ в embedded-tech-spec сохранена как часть нотации ТЗ.
- SKILL.md переведены на LF в рабочей копии.

### Удалено

- Сведения о конкретном стенде из поля `compatibility` в stm32-swo-trace и stm32-rtt-console.

### Исправлено

- Битая ссылка `assets/vscode/` в demo-stm32-cmake-example-builder (теперь `references/VSCODE.md`).
- Ссылка на отсутствующий `references/COMMANDS.md` в stm32-cubemx-codegen (теперь отсылка к UM1718)
  и заголовок навыка, не совпадавший с его именем.
- Ссылки на несуществующий аккаунт `your-username` и ошибочное поле `meta` в примере README.
