# Памятка: частые команды и проблемы

Сюда заглядывают разработчик и агент, когда что-то не работает, прежде чем искать
новое решение. Для каждой проблемы указано, как её исправить и как вернуть прежнее
состояние.

Условные обозначения: **PS** — PowerShell на Windows, **sh** — оболочка Linux
(Orange Pi, WSL). Команды git одинаковы в обеих, кроме кавычек (раздел «Псевдоним
`git land`»).

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
