# Changelog

Все заметные изменения в этом проекте документируются в этом файле.
Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [Unreleased]

### Добавлено

- Навык stm32-diag-record: запись полей прошивки по SWD в mcap или bag без ROS (OpenOCD без сброса цели,
  ключи `--target`, `--interface`, `--reset`, групповое чтение `mdw`, фактическая частота, сохранение файла
  при Ctrl+C), пример полей для прошивки-зонда.
- Сайт: страницы stm32-renode-emulator, stm32-ros-alt-channel и embedded-tech-spec (путь данных, шаги, что
  проверено в CI и что нет); карточки на главной.
- `.github/FUNDING.yml`: ссылка на канал t.me/mcu_cicd_hil.
- Сайт на GitHub Pages (`site/`, workflow Pages): тема в стиле GitHub, тёмная по умолчанию, круглая кнопка
  «тёмная, светлая, авто»; первая страница — инфографика stm32-diag-record; Kits проверяет ссылки сайта.
- stm32-diag-record 1.4.0: режим `--backend rtt-timer` (прототип) — аддитивная заглушка на свободном таймере
  пишет поля в RTT, хост читает через `rtt server`; быстрее базового опроса, флеш не меняется, перехват
  снимается в конце. Универсальная заглушка (Cortex-M3/M4), параметры стендов в `scripts/rtt/boards.toml`
  (f103c8, f401cc, f411ce, f429zi, at32f403a; f030r8 — нет VTOR). Emulation прогоняет настоящую заглушку в
  Renode (прерывание через NVIC), Kits проверяет разбор кадров. План: R21.
- stm32-diag-record 1.3.0: доктор не привязан к ST-Link и проверяет совместимость с теми же ключами, что и запись
  (конфигурации OpenOCD, пробник, ядро и состояние цели, адреса по ELF, чтение слова без остановки ядра);
  ключ `--openocd-command`; в SKILL.md — синтаксис спецификации полей (откуда берётся каждый ключ) и раздел
  «Другие MCU» с порядком проверки; пример полей в форме `&структура.поле`.
- План исследований: R19 (первая запись stm32-diag-record на железе) и R20 (другие Cortex-M, WeAct BlackPill
  AT32).
- stm32-diag-record 1.2.0: `--backend renode` читает поля из работающего Renode через его монитор (`--port`),
  без платы и без остановки эмуляции; Emulation пишет так mcap из настоящего Renode с зондом.
- Общие модули навыков: оригинал в stm32-ros-alt-channel, копии обновляет `ci/sync_shared.py`, задание Kits
  проверяет совпадение.
- Emulation: stm32-diag-record пишет поля зонда через поддельный OpenOCD (`ci/fake_openocd.py`), файлы читаются
  пакетами `mcap` и `rosbags`; Kits проверяет преобразования record_diag.py.
- План повторных исследований навыков на стенде: `docs/research-plan.md`.
- Прошивка-зонд для исследований вынесена в отдельный репозиторий stm32-skills-probe (платы F411CE, F401CC,
  F103C8); план исследований обновлён под эти платы.
- Kits проверяет наличие RTT-команд в OpenOCD, используемых stm32-rtt-console.
- Задания CI Kits и Emulation (`ci/run_checks.py`, закреплённые загрузки `ci/tools.lock.json`):
  самопроверки комплектов навыков; прошивка-зонд stm32-skills-probe (закреплённый коммит) собирается для
  F411CE, F401CC и F103C8 (F411CE — тулчейн-файлом stm32-vscode-cmake-scaffold), запускается в Renode 1.17 и
  QEMU через stm32-renode-emulator с проверкой стартового лога; модели сверяются с SVD, stm32-ros-alt-channel
  разрешает адреса (в том числе поле структуры по DWARF) и читает память работающего ядра.
- stm32-renode-emulator: макет `stm32g4_mock.repl`, сценарии `boot.resc` и `debug.resc`, проверка лога
  `check_startup_log.py` и образец ожиданий — комплект теперь самодостаточен.

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
- stm32-bsp-driver-pattern (0.2): SKILL.md сокращён с 775 до 220 строк; шаблоны заголовка,
  реализации, IO-уровня и сценарии вынесены в `references/`, добавлен раздел «Порядок работы».

### Удалено

- Сведения о конкретном стенде из поля `compatibility` в stm32-swo-trace и stm32-rtt-console.

### Исправлено

- stm32-ros-alt-channel: `resolve_addresses.py` склеивал поля с одинаковым `expr` в одно; лишнее определение
  `add_connection` в `bag_writer.py`.
- stm32-ros-alt-channel: `resolve_addresses.py` падал без `--svd`, даже если полей `svd:` в спецификации нет.
- stm32-ros-alt-channel: следы стенда в узлах: нейтральные имена узлов (`stm32_alt_channel`, `stm32_recorder`),
  `all` в `recorder_node.py` выбирает все топики, кроме `/rosout*`.

- stm32-renode-emulator: `run.py` искал макет и проверку лога в каталогах исходного проекта; обрезанный
  SKILL.md переписан (маршруты, прогон, проверка лога, версии Renode).
- weact-logic-analyzer: восстановлено обрезанное тело SKILL.md (разделы 1–9) и разделы о работе на
  удалённом стенде по SSH и поиске начала обмена.
- stm32-rtt-console, stm32-swo-trace: восстановлены обрезанные таблицы (выбор варианта RTT с замерами,
  порядок проверки SWO); в rtt добавлены команды OpenOCD, проверка совместимости и требования к проекту,
  в swo — запуск скрипта; убраны серийный номер пробника и сведения о конкретной плате.
- stm32-rtt-console: shell-скрипты падали в `sh`, если не установлено окружение stm32-gdbtest;
  справка по `rtt setup/start` запрашивалась без цели и была пустой.
- stm32-ros-alt-channel: пути к SVD, ELF и корню проекта задаются аргументами и переменными окружения
  вместо путей исследовательского проекта; убраны личный путь к тулчейну и порт `COM7` по умолчанию.
- stm32-rtt-console: скрипты считают корнем проекта текущий каталог, а не каталог на 2–4 уровня выше;
  убрана привязка к SRAM конкретного стенда.

- Битая ссылка `assets/vscode/` в demo-stm32-cmake-example-builder (теперь `references/VSCODE.md`).
- Ссылка на отсутствующий `references/COMMANDS.md` в stm32-cubemx-codegen (теперь отсылка к UM1718)
  и заголовок навыка, не совпадавший с его именем.
- Ссылки на несуществующий аккаунт `your-username` и ошибочное поле `meta` в примере README.
