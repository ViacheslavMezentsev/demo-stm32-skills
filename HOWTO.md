# Памятка: частые команды и проблемы

Сюда заглядывают разработчик и агент, когда что-то не работает, прежде чем искать
новое решение. Для каждой проблемы указано, как её исправить и как вернуть прежнее
состояние.

Условные обозначения: **PS** — PowerShell на Windows, **sh** — оболочка Linux
(Orange Pi, WSL). Команды git одинаковы в обеих, кроме кавычек (раздел «Псевдоним
`git land`»).

## Проверки CI локально

Workflow Skills состоит из трёх заданий: Validate (`skills-ref`), Kits и Emulation. Два последних
запускаются тем же скриптом, что и в CI (Linux или WSL, Python 3.11+):

```sh
python3 ci/run_checks.py kits                 # скрипты, assets, самопроверки навыков (без сети)
python3 ci/run_checks.py emulation            # зонд, GCC, Renode 1.17 и SVD из ci/tools.lock.json, QEMU
python3 ci/run_checks.py emulation --probe ../stm32-skills-probe   # с локальной копией зонда
python3 ci/run_checks.py kits emulation --strict   # как в CI: SKIP считается ошибкой
```

Необязательные инструменты дают SKIP: `sigrok-cli`, `clang-format` ≥ 16, `openocd`, `qemu-system-arm`, GDB с
Python (`gdb-multiarch` или `arm-none-eabi-gdb-py3`). Ubuntu: `sudo apt install sigrok-cli clang-format
openocd qemu-system-arm ninja-build gdb-multiarch`. Загрузки (`ci/fetch_tools.py`, около 170 МБ) кэшируются в
`build/ci-tools` и проверяются по SHA-256; зонд берётся из git по закреплённому коммиту (`probe` в
`ci/tools.lock.json`, проверка — сам хеш коммита). Итог — `build/ci/summary.json`, журналы — `build/ci/*.log`.

Обновить зонд в CI: изменения зонда сначала попадают в его `main` (или хотя бы отправленную ветку: при
слиянии перемоткой хеш не меняется), затем в `ci/tools.lock.json` меняется `commit` и `version`. До публикации
изменения зонда проверяются ключом `--probe <локальная копия>`.

| Проблема | Решение |
| --- | --- |
| `SHA-256 mismatch` при загрузке | Файл повреждён или подменён: он уже удалён, повторить запуск; если повторяется — сверить `url` и `sha256` в `ci/tools.lock.json` |
| Renode: `GDB server ... started`, но порт недоступен | В окружении нет IPv6 на loopback (часть песочниц и контейнеров); CI читает память монитором Renode, GDB-сервер для проверок не нужен |
| Прогон QEMU заканчивается таймаутом | Прошивка не вызывает семихостинг `SYS_EXIT`: QEMU сам не останавливается (у зонда — сборка с `-DPROBE_EXIT_MS`) |
| `upload-pack: not our ref <хеш>` при загрузке зонда | Закреплённый коммит ещё не отправлен на GitHub: отправить ветку зонда или проверять с `--probe` |

Эмуляция — не аппаратная проверка: PASS в Renode или QEMU подтверждает только работу в эмуляторе.

## Общие модули навыков

Навык копируют в проект отдельной папкой, поэтому общий код лежит в каждом навыке, который им пользуется.
Оригинал один, остальные — побайтные копии; список — `SHARED` в `ci/sync_shared.py`. Сейчас оригинал —
`stm32-ros-alt-channel/scripts/` (`resolve_addresses.py`, `svd_lookup.py`, `bag_writer.py`, `mcap_writer.py`,
`msg_types.py`, `openocd_link.py`, `field_values.py`, `gdb_tools.py`, `process_tools.py`), копии — в `stm32-diag-record/scripts/`.

```sh
python3 ci/sync_shared.py            # после правки оригинала: обновить копии
python3 ci/sync_shared.py --check    # то же проверяет задание Kits
```

| Проблема | Решение |
| --- | --- |
| Kits: «копии отличаются от оригинала» | Правка попала в копию или не синхронизирована: перенести её в оригинал, запустить `sync_shared.py`, закоммитить оригинал и копии вместе |

Симлинки и подмодули вместо копий не используются: на Windows и при копировании навыка в проект они теряются.

## Сайт на GitHub Pages

Страницы лежат в `site/`: статический HTML без сборки и внешних скриптов, общая тема — `site/assets/site.css`
(цвета GitHub, тёмная по умолчанию) и `site/assets/theme.js` (круглая кнопка «тёмная, светлая, авто», выбор
хранится в браузере). Workflow `Pages` публикует `site/` при push в main, если менялись `site/**` или сам
workflow; проверка ссылок — `check_site` в `ci/run_checks.py` (задание Kits и шаг перед публикацией).

Включить один раз: Settings -> Pages -> Build and deployment -> Source: GitHub Actions. Адрес:
https://viacheslavmezentsev.github.io/demo-stm32-skills/

```sh
python3 -m http.server -d site 8000   # просмотр локально: http://localhost:8000
```

Новая страница навыка — `site/skills/<навык>/index.html` с той же шапкой (ссылки на `../../assets/`) и
карточка в `site/index.html`.

## Git: рабочий цикл без PR

### Локальная проверка SSH-подписи

Ошибка `gpg.ssh.allowedSignersFile needs to be configured` при `git verify-commit`
не означает отсутствие подписи: Git нужен список доверенных публичных ключей.
Создайте локальный файл вне отслеживаемых файлов (например, в `build/`) со строкой
`<email> namespaces="git" ssh-ed25519 <публичный-ключ>` из заранее проверенного
публичного Signing Key. Затем выполните:

```text
git -c gpg.ssh.allowedSignersFile=build/allowed-signers verify-commit HEAD
```

Настройка действует только для этой команды; откат не нужен. Не добавляйте ключи
или локальный список в Git. Успех проверяет подпись относительно выбранного ключа,
но не подтверждает его регистрацию в GitHub и статус Verified на сервере.

### Последовательность работы

Ветка `<агент>/<задача>` создаётся от свежего main и после проверок сливается в main
перемоткой (fast-forward). Порядок действует для одного разработчика с агентами; при расширении команды его пересматривают. Слить без PR в веб-интерфейсе GitHub нельзя, и
автоудаление веток GitHub работает только для PR, поэтому слияние и уборка делаются
локально.

```
git switch main
git pull --ff-only
git switch -c claude/<задача>          # новая ветка
# … коммиты …
git push -u origin claude/<задача>     # CI: дождаться зелёного Skills / Validate
git land claude/<задача>               # перемотать main, отправить, удалить ветку
```

Перемотка не создаёт merge-коммит: в main попадают те же подписанные коммиты, и
GitHub показывает их Verified. Если ветка отстала от main, `git land` остановится на
`--ff-only`, ничего не изменив. Тогда ветку переносят на свежий main и снова
подписывают:

```
git switch claude/<задача>
git rebase -S origin/main              # конфликты: исправить, git add, git rebase --continue
git push --force-with-lease            # только для своей неслитой ветки
```

Force push в main и в чужие ветки запрещён.

### Pull request внешнего участника

PR не сливается кнопкой в GitHub: коммиты переносятся на свежий main с подписью
владельца (автор сохраняется) и попадают в main перемоткой.

```
git fetch origin pull/<N>/head:pr/<N>
git switch pr/<N>
git rebase -S origin/main
git push -u origin pr/<N>              # CI: дождаться зелёного Skills / Validate
git land pr/<N>
```

GitHub не узнаёт перенесённые коммиты, поэтому PR закрывают вручную со ссылкой на
коммит в main. Вернуть: до `git land` — `git switch main && git branch -D pr/<N>`.

### Ветка агента из клона `../.work`

Агент не коммитит в рабочую копию владельца. Рядом с репозиториями лежит скрытая
папка `.work`: в ней ключ подписи агента (общий) и отдельный клон для каждого
репозитория. Папка вне репозиториев, поэтому в `.gitignore` её не добавляют; в
облачную синхронизацию её не кладут.

```text
<папка с репозиториями>\
  .work\
    keys\claude_signing, claude_signing.pub   # SSH-ключ только для подписи
    demo-stm32-skills\                        # клон агента
  demo-stm32-skills\                          # рабочая копия владельца
```

Открытый ключ `claude_signing.pub` добавляется в GitHub как **Signing Key** (не
Authentication Key): им нельзя выполнить push, отзывается он удалением в Settings →
SSH and GPG keys. В клоне агента remote `owner` указывает на рабочую копию владельца
(`../../demo-stm32-skills`), подпись включена локально (`commit.gpgsign`, `gpg.format
ssh`, `user.signingkey`), committer — владелец с подтверждённым в GitHub email,
участие агента отмечается строкой `Co-Authored-By`.

Владелец принимает ветку из корня своего репозитория:

```
git fetch ../.work/demo-stm32-skills claude/<задача>:claude/<задача>
git log --oneline main..claude/<задача>
git push -u origin claude/<задача>     # CI: дождаться зелёного Skills / Validate
git land claude/<задача>
```

Относительный путь `../.work` работает только из копии, рядом с которой лежит `.work`.
Из WSL копия на диске Windows видна как `/mnt/d/…`, а `~/github/…` — это отдельная
папка Linux; там путь указывают полностью (буква диска — своя):

```sh
git fetch /mnt/d/<папка с репозиториями>/.work/demo-stm32-skills claude/<задача>:claude/<задача>
git -c gpg.ssh.allowedSignersFile=/mnt/d/<папка с репозиториями>/.work/keys/allowed_signers \
    log --show-signature --oneline main..claude/<задача>
```

| Проблема | Решение |
| --- | --- |
| `'../.work/…' does not appear to be a git repository` | Команда запущена не из той копии (например, `~/github` в WSL вместо `/mnt/d/…`): `pwd`, затем полный путь к клону |
| `detected dubious ownership in repository` (WSL, диск Windows) | `git config --global --add safe.directory <полный путь к клону>` |
| `git switch` отказывается: локальные правки будут перезаписаны | Если те же правки уже в ветке агента: `git stash push -u`, переключиться, сверить, затем `git stash drop` |

`fetch` переносит коммиты без изменений, вместе с подписями. Ветку с тем же именем
в своей копии перед повторным приёмом обновляют тем же `fetch` (только перемотка;
при расхождении — `+claude/<задача>:claude/<задача>`, если своих коммитов в ней нет).
После `git land` агент синхронизирует клон: `git fetch owner && git switch main &&
git merge --ff-only owner/main && git branch -d claude/<задача>`.

Вернуть: удалить принятую ветку — `git branch -D claude/<задача>`; отключить ключ
агента — удалить его в GitHub; убрать клон — удалить папку `.work\<репозиторий>`.

### Псевдоним `git land`

Установка — одинарные кавычки обязательны: в PowerShell внутри двойных кавычек `$`
подставляется самим PowerShell, а `\"` не экранирует кавычку.

```powershell
git config --global --unset-all alias.land   # если уже был; ошибка «no such section» не страшна
git config --global alias.land '!f() { b=${1:-$(git branch --show-current)}; git fetch origin && git switch main && git merge --ff-only origin/main && git merge --ff-only $b && git push --atomic origin main :$b && git branch -d $b; }; f'
git config --global --get-all alias.land     # ровно одна строка с b=${1:-$(git branch --show-current)}
```

Ту же команду в sh можно выполнить без изменений. Что делает `git land <ветка>`:
`fetch` → перемотка main до `origin/main` → перемотка main до ветки → одним атомарным
push отправка main и удаление ветки на GitHub → удаление локальной ветки.

| Проблема | Решение |
| --- | --- |
| `syntax error: unexpected end of file`, в ошибке `b=;` | Псевдоним записан в двойных кавычках из PowerShell. Удалить (`--unset-all`) и записать заново в одинарных |
| `warning: alias.land has multiple values` | `git config --global --unset-all alias.land`, затем записать заново; либо `git config --global --edit` и удалить лишние строки `land = …` |
| `fatal: Not possible to fast-forward` | Ветка отстала от main: `git rebase -S origin/main` (выше) |
| Push отклонён: protected branch, required pull request | В Settings → Rules снять требование PR для main или разрешить себе обход (bypass) |

Вернуть как было: удалить псевдоним — `git config --global --unset-all alias.land`.

### Неотправленные ветки и порядок push

```
git fetch --prune
git for-each-ref --format="%(refname:short) %(upstream:short) %(upstream:track)" refs/heads
git log --oneline --graph --branches --not --remotes   # коммиты, которых нет ни в одной ветке GitHub
git rev-list --count origin/main..<ветка>              # сколько коммитов ветки ещё не в main
git merge-base --is-ancestor <ветка-A> <ветка-B> && echo "A — основа B"
```

Пустой upstream — ветки на GitHub нет. Первой пушится ветка-основа (её коммиты входят в следующую), затем
ветки поверх неё: `git push -u origin <основа>`, потом `git push -u origin <ветка>`. Так CI каждой ветки
проверяет только её изменения.

### Уборка веток

```
git fetch --prune                                  # убрать ссылки на удалённые на GitHub ветки
git branch -r --merged origin/main                 # слитые ветки на GitHub
git push origin --delete <ветка> [<ветка> …]       # удалить на GitHub
git branch -vv                                     # локальные; [gone] — на GitHub уже нет
git branch -d <ветка>                              # удалить слитую локальную
git branch -D <ветка>                              # удалить, если хэш отличается, а содержимое уже в main
```

На другом компьютере (Orange Pi) после слияния:

```sh
git switch main && git pull --ff-only && git branch -D <ветка> && git fetch --prune
```

### Как вернуть состояние

| Ситуация | Команда |
| --- | --- |
| Отменить незакоммиченные правки в файлах | `git restore <файл>` или `git restore .` |
| Убрать файл из индекса, сохранив правки | `git restore --staged <файл>` |
| Удалить неотслеживаемые файлы | сначала `git clean -n` (что удалится), затем `git clean -f`; `*.local.toml` игнорируются и не удаляются без `-x` — не используйте `-x` |
| Отменить последний неотправленный коммит, сохранив правки | `git reset --soft HEAD~1` |
| Локальный main испорчен, но ещё не отправлен | `git switch main && git reset --hard origin/main` |
| Удалили ветку по ошибке | `git reflog` → найти хэш → `git branch <ветка> <хэш>`; на GitHub вернуть: `git push origin <ветка>` |
| Отменить коммит, уже отправленный в main | `git revert <хэш>` новым подписанным коммитом, затем обычный цикл; историю main не переписывать |
| Прервать неудачный rebase или merge | `git rebase --abort` / `git merge --abort` |

### Подпись и Verified

```
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519_signing.pub
git config --global commit.gpgsign true
git log --show-signature -1            # проверить подпись последнего коммита
```

Ключ добавляется в GitHub как **Signing Key** (отдельно от Authentication Key), email
автора должен быть подтверждённым адресом аккаунта. Unverified означает: коммит не
подписан, подписан другим ключом или email не совпадает. Исправление для неотправленных
коммитов — `git commit --amend -S --no-edit` (последний) или `git rebase -S origin/main`
(все коммиты ветки). Вернуть: `git config --global --unset commit.gpgsign`.

### Концы строк и служебные файлы

- На Windows `core.autocrlf=true`: в рабочей копии CRLF, в репозитории LF. Файлы
  `.github/**`, `*.sh`, `*.S` всегда LF (`.gitattributes`).
- Все файлы показаны изменёнными без реальной правки — концы строк: проверить
  `git diff --stat` с `-c core.autocrlf=true`; после правки `.gitattributes` —
  `git add --renormalize .`.
- `fatal: Unable to create '…/.git/index.lock': File exists` — убедиться, что git не
  запущен (IDE, другой терминал), затем удалить `.git/index.lock`.

## RTT: сохранить доказательства и разобрать начало записи

rtt-timer сохраняет исходные `/_diag/rtt/seq`, `/_diag/rtt/tick_ms` внутри mcap/bag и `<имя>.rtt.json`
рядом. Храните запись вместе с этим JSON и журналом OpenOCD. `dropped_est` относится только к разрывам
между принятыми кадрами, а `record_closed` не подтверждает снятие перехвата. При одинаковом tick
не удаляйте кадры: проверьте seq и остальные значения. Начальный укороченный период может быть
следствием halt/resume и pending IRQ; механизм и порядок подтверждения —
[в описании RTT](stm32-diag-record/references/rtt-evidence.md).

Проверка без платы: `python3 ci/test_rtt_record.py`; с установленными `mcap` и `rosbags` —
`python3 ci/test_rtt_record.py --readers`. Это синтетический транспорт с настоящими писателями файлов,
а не аппаратная проверка таймеров и снятия перехвата.

### RTT: зависание после снятия перехвата

`running` в OpenOCD означает исполнение инструкций, включая бесконечный Default_Handler. Возврат
VTOR/RCC и очистка pending не доказывают выхода из активного ISR: проверьте IABR и продвижение tick.
В 1.7.0 это делает GDB-завершение (`--cleanup-gdb`, `--cleanup-gdb-port 3334`). При отказе сохраните
mcap/bag, `.rtt.json`, `.teardown.json`, `.gdb.log` и `.openocd.log` до сброса платы. После фиксации
диагностики восстановите известную прошивку сбросом; не используйте подмену PC как штатное завершение.
Проверки без платы: `python3 ci/test_rtt_cleanup.py` и `python3 ci/test_rtt_record.py`.

Если после GDB `detach` tick неподвижен, TIM3 не активен, а в DHCSR установлен S_HALT (бит 17),
ядро осталось остановленным отладчиком. `detach` не заменяет явный OpenOCD `resume`.
Рекордер выполняет его после успешного GDB-восстановления и проверяет tick и снятие S_HALT;
при ошибке GDB возобновление запрещено. Регрессионные тесты моделируют оба результата resume.

Нулевой IABR при остановленном ядре не оказался достаточным для снятия RTT-перехвата на F411CE:
после resume зафиксирован active TIM3 при уже выключенном IRQ. Перед возвратом VTOR рекордер
теперь исполняет прошивку 250 мс с SRAM-вектором, проверяет tick и IRQ, затем восстанавливает вектор.
Поля `*_quiesce` помогают отличить отказ на этом этапе от отказа после окончательного resume.
Это защитный протокол; точная причина позднего входа ещё исследуется.

Если Ctrl+C сохраняет запись, но снятие перехвата завершается WinError 10053, проверьте журнал
OpenOCD: `shutdown command invoked` до подключения GDB указывает на преждевременный выход сервера.
Рекордер запускает OpenOCD и GDB с `CREATE_NEW_PROCESS_GROUP` на Windows и `start_new_session`
на POSIX: Ctrl+C останавливает чтение в Python, сервер остаётся доступен для восстановления.
Закрытие окна консоли и Ctrl+Break этим протоколом не покрываются.

### CI: запись из монитора Renode на медленном runner

`--rate` задаёт желаемую частоту опроса, но скорость команд монитора и нагрузка runner могут
снизить число циклов за `--seconds`. Функциональный тест не должен требовать заданную пропускную
способность: он проверяет минимум два полных цикла, восемь каналов, рост и монотонность счётчиков.
При отказе смотрите `build/ci/diag-renode/record.log` и `record.mcap`; прежняя ошибка выводила
лишь первые шесть значений, скрывая реальное число циклов и конкретное условие отказа.
Регрессионная проверка без Renode: `python3 ci/test_diag_renode_samples.py`.

### Общий транспорт: проверять оба потребителя

После изменения общей базы запустите `python3 ci/sync_shared.py`, затем Kits и Emulation.
Kits запускает тесты каждой самодостаточной копии, включая ROS-ноду с подменой middleware и
реальным TCP-сервером OpenOCD для тестов. Emulation дополнительно использует адреса из ELF
закреплённого stm32-skills-probe и независимые читатели MCAP/bag. Это не аппаратная проверка
ROS/WSL или rosserial: их отдельно проверяют на стенде. `GDB` и `--cleanup-gdb` теперь проверяются
на поддержку Python; ошибочный явный путь не заменяется другим отладчиком молча.

Проверка с настоящим ROS Noetic (локально, без платы и существующего ROS-master):

```sh
source /opt/ros/noetic/setup.bash
python3 ci/test_ros_live.py
```

Тест запускает свой master на свободном порту, настоящую telemetry_node и подписчиков;
OpenOCD заменён TCP-имитатором. После теста его процессы завершаются. Прогон подтверждён
в WSL 2026-10-19: знаковое поле −1, поле со сдвигом 18. Полный Windows/WSL-маршрут и UART
этим тестом не покрываются.

## ROS: частота своих топиков и независимая проверка времени

`ros1_channel.py --via openocd --keep --hz-topic /bp/tick_ms` проверяет опубликованный топик.
Без `--hz-topic` выбирается поле из опубликованных и разрешённых `--topics`, предпочтительно `*/tick_ms`.
Список больше не обрезается до восьми строк. Отсутствие топика/частоты даёт код 1, а не ложный успех.
`timeout` с кодом 124 допустим только если получена измеренная частота. Подсказка echo использует тот же топик.

Перед точными измерениями прочитайте [методику времени](stm32-ros-alt-channel/references/time-validation.md).
Исследование 19, §14, опровергло ранний вывод «ВМ стоит»: часы замедлялись и прыгали из-за конфликтующих
служб коррекции. Сверяйте WSL с Windows и MCU с Windows независимо; настройки NTP автоматически не меняются.
Для регрессии: `python ci/test_ros_clock_validation.py`; в WSL с ROS Noetic:
`source /opt/ros/noetic/setup.bash && python3 ci/test_ros_live.py` — настоящий rostopic hz на частном мастере,
TCP-имитатор OpenOCD, без платы. Общий CI: `python3 ci/run_checks.py kits emulation --strict`.

При сравнении HSI/HSE сохраняйте делители и нормируйте результат на ожидаемую частоту:
на проверенном F411CE с LOAD=15999 прямой HSE 25 МГц даёт 1562,5 tick/с, а не 1000.
Обязательны ограниченные ожидания HSERDY/SWS, восстановление исходных настроек и отдельное
чтение tick/fault после отключения GDB. Окончательный результат HSI→HSE→HSI и ограничения
приведены в [методике](stm32-ros-alt-channel/references/time-validation.md#контроль-hsi--hse-25-мгц--hsi-на-f411ce).

Инфографика diag-record разделяет A (опрос mdw с хоста) и B (выборка IRQ на MCU и передача RTT).
Свойства A «без добавленного кода/без halt» нельзя переносить на B. Оба используют SWD;
RTT сохраняет выборки, а не каждое событие между ними. Renode — отдельный вариант без платы.

Для GDB-опытов с работающим ядром `monitor resume` не является безусловно повторяемой командой:
OpenOCD может ответить `target not halted` / `Protocol error with Rcmd`. Сначала прочитайте
DHCSR.S_HALT (бит 17), вызывайте resume только для остановленного ядра, затем подтвердите рост tick.
Совпадение регистров и успешное завершение сценария проверяются отдельно: первый опыт LSE восстановил
BDCR, но получил ошибку на лишнем resume; успешный повтор и независимая проверка описаны в
[методике времени](stm32-ros-alt-channel/references/time-validation.md).


Для бинарного UART во внешнюю программу используйте TCP-терминал Renode с отключённым Telnet:
`emulation CreateServerSocketTerminal 54321 "protocol" false`, затем
`connector Connect sysbus.usart1 protocol` (путь UART зависит от платформы).
Создание и подключение подтверждены в Renode 1.17.0 на модели зонда; сквозной обмен пока не проверен.
Для Windows COM-приложения дополнительно нужен двунаправленный TCP/serial-мост и пара com0com.
Сырой RTU через TCP не является Modbus TCP, а интервалы поступления TCP-данных не доказывают
соблюдение межбайтовых интервалов RTU. Полный сценарий и критерии — [R22](docs/research-plan.md).
