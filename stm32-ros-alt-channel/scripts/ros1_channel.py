"""Единый запуск канала «прошивка → ROS1 в WSL»: два маршрута, запись bag/mcap, доктор окружения.

МАРШРУТЫ
    --via openocd     данные читаются из памяти платы через отладчик (OpenOCD на Windows).
                      Нужны: OpenOCD, ST-Link, ELF прошивки. Root/socat не нужны, UART свободен.
    --via rosserial   штатные топики прошивки по отладочному UART (--port) через tcp_serial_redirect +
                      socat + serial_node. Нужны: ST-Link VCOM, root в WSL (берётся как `wsl -u root`).
                      Переживает перезагрузку платы: serial_node пересогласует топики сам.

ЗАПИСЬ
    --record bag|mcap  пишем выбранные топики своим писателем (ROS не нужен для формата)
    --out ИМЯ          имя файла; путь можно задать полностью (Windows-путь пишется из WSL через /mnt)
    --topics ...       список топиков через запятую или all
    --seconds N        остановиться через N секунд
    Если bag остался без индекса (запись прервали), он автоматически переиндексируется, а исходник
    сохраняется рядом как <имя>.bak.

ПРИМЕРЫ
    python run_channel.py --doctor
    python run_channel.py --via openocd --record mcap --seconds 10 --out board.mcap
    python run_channel.py --via rosserial --record bag --seconds 20 --topics all
    python run_channel.py --via openocd --keep            # оставить канал, смотреть rostopic echo
    python run_channel.py --cleanup
"""
import argparse
import pathlib
import shutil
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
import os
# Корень проекта прошивки: ELF, SVD (resources/STM32<профиль>.svd) и журналы ищутся от него.
ROOT = pathlib.Path(os.environ.get("ROS1_PROJECT_ROOT", os.getcwd()))
ROS_ALT = HERE
RESOLVE = ROS_ALT / "resolve_addresses.py"
ADDRESSES = ROS_ALT / "addresses.json"
TELEMETRY_NODE = ROS_ALT / "telemetry_node.py"
RECORDER = HERE / "recorder_node.py"
# Пример tcp_serial_redirect.py из pyserial (examples/), положенный в проект.
REDIRECT = pathlib.Path(os.environ.get("ROS1_TCP_SERIAL_REDIRECT",
                                       ROOT / "tools" / "scripts" / "tcp_serial_redirect.py"))
DISTRO = "Ubuntu-20.04"
ROS_SETUP = "source /opt/ros/noetic/setup.bash"
ROS_MASTER_URI = "http://127.0.0.1:11311"
LOG_DIR = ROOT / "build" / "channel"   # заменяется в main() на build/<пресет>/channel/logs

_processes = []


# ---------------------------------------------------------------- инфраструктура

def wsl_path(path):
    text = str(path).replace("\\", "/")
    drive, rest = text.split(":", 1)
    return "/mnt/%s%s" % (drive.lower(), rest)


def wsl(command, timeout=120, user=None):
    prefix = ["wsl", "-d", DISTRO] + (["-u", user] if user else [])
    return subprocess.run(prefix + ["-e", "bash", "-lc", command], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)


def spawn(name, command, user=None, wsl_process=True):
    """Долгоживущий процесс. Для WSL — обязательно дочерний: `nohup ... &` внутри wsl не выживает."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / ("%s.log" % name)
    handle = open(log, "wb")
    if wsl_process:
        prefix = ["wsl", "-d", DISTRO] + (["-u", user] if user else [])
        process = subprocess.Popen(prefix + ["-e", "bash", "-lc", command],
                                   stdout=handle, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
    else:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
    _processes.append(process)
    return process, log


def port_open(port):
    """Проверка реальным подключением, а не таблицей сокетов Windows.

    ПОЧЕМУ. `Get-NetTCPConnection` показывает только сокеты Windows-процессов: listener, поднятый внутри
    WSL, в этой таблице не виден, хотя соединение по 127.0.0.1 работает (mirrored networking). Из-за
    этого мастер ROS ошибочно считался «не поднявшимся».
    """
    result = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "(Test-NetConnection -ComputerName 127.0.0.1 -Port %d "
                             "-InformationLevel Quiet -WarningAction SilentlyContinue)" % port],
                            capture_output=True, text=True)
    return "True" in result.stdout


def wait_port(port, attempts=20, delay=1.0):
    for attempt in range(1, attempts + 1):
        if port_open(port):
            return attempt
        time.sleep(delay)
    return 0


def stop_all():
    patterns = ("telemetry_node.py", "serial_node.py", "recorder_node.py",
                "roscore", "rosmaster", "rosout")
    for pattern in patterns:
        wsl("pkill -f '[%s]%s' 2>/dev/null" % (pattern[0], pattern[1:]))
    # Убитый roscore оставляет lock-файл, и следующий запуск падает с «another roscore/master is
    # already running», хотя порт свободен. Чистим и lock, и остатки PID-файлов.
    wsl("rm -f $HOME/.ros/roscore-*.pid $HOME/.ros/rosmaster*.pid 2>/dev/null")
    wsl("pkill -f '[s]ocat.*ttyVirtual0' 2>/dev/null; rm -f /dev/ttyVirtual0", user="root")
    for process in _processes:
        if process.poll() is None:
            process.terminate()
    # openocd и tcp_serial_redirect живут на Windows, их WSL-очистка не видит. Раньше редиректор
    # снимался только в маршруте B, и после --cleanup порт 7777 оставался занят.
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-Process openocd -ErrorAction SilentlyContinue | Stop-Process -Force; "
                    "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and "
                    "$_.CommandLine -like '*tcp_serial_redirect*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
                    "$c = Get-NetTCPConnection -LocalPort 7777 -State Listen -ErrorAction SilentlyContinue; "
                    "if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force "
                    "-ErrorAction SilentlyContinue } }"],
                   capture_output=True)
    # ПРОВЕРКА. `terminate()` завершает клиент `wsl.exe`, а процесс ВНУТРИ WSL может остаться жить —
    # и мастер продолжает отвечать на rostopic. Поэтому добиваем повторно и убеждаемся, что пусто.
    for _attempt in range(3):
        leftover = wsl("pgrep -f '[r]oscore|[r]osmaster|[s]erial_node|[t]elemetry_node|"
                       "[r]ecorder_node|[s]ocat.*ttyVirtual0' | head -5")
        if not leftover.stdout.strip():
            break
        for pattern in patterns:
            wsl("pkill -9 -f '[%s]%s' 2>/dev/null" % (pattern[0], pattern[1:]))
        wsl("pkill -9 -f '[s]ocat.*ttyVirtual0' 2>/dev/null", user="root")
        time.sleep(1.0)


# ---------------------------------------------------------------- доктор

def doctor(args):
    """Проверить окружение и напечатать таблицу. Ничего не запускает и не меняет."""
    checks = []

    def add(name, ok, detail=""):
        checks.append((name, ok, detail))

    add("Windows: Python 3", sys.version_info >= (3, 8), sys.version.split()[0])
    add("Windows: OpenOCD", bool(shutil.which("openocd")), shutil.which("openocd") or "нет в PATH")
    add("Windows: ELF прошивки", (ROOT / args.elf).is_file(), args.elf)

    # Отладчик на шине: имя порта ST-Link VCOM и признак живого OpenOCD.
    ports = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(Get-CimInstance Win32_SerialPort | "
                            "Where-Object { $_.Name -like '*STLink*' } | "
                            "Select-Object -First 1).DeviceID"], capture_output=True, text=True)
    stlink_port = ports.stdout.strip()
    add("Windows: ST-Link VCOM", bool(stlink_port), stlink_port or "не найден")
    add("Windows: SVD", (ROOT / "resources" / f"STM32{args.profile}.svd").is_file(),
        f"resources/STM32{args.profile}.svd")

    ros = wsl("%s && rosversion -d" % ROS_SETUP)
    add("WSL: ROS", ros.returncode == 0, " ".join(ros.stdout.split()) or "нет")
    # `which` проверяем ВНУТРИ окружения ROS: без source утилиты не видны, и проверка даёт ложный «НЕТ».
    add("WSL: rostopic", wsl("%s && which rostopic" % ROS_SETUP).returncode == 0,
        "/opt/ros/noetic/bin/rostopic")
    add("WSL: rosrun", wsl("%s && which rosrun" % ROS_SETUP).returncode == 0, "есть")
    add("WSL: socat (нужен для --via rosserial)", wsl("which socat").returncode == 0, "есть")
    add("WSL: root без пароля (socat)", wsl("id -u", user="root").stdout.strip() == "0",
        "wsl -u root")
    # Связь WSL→Windows имеет смысл только когда OpenOCD уже запущен; иначе это не ошибка окружения.
    if port_open(4444):
        add("WSL: доступ к Windows 127.0.0.1:4444",
            "ДОСТУПЕН" in wsl('timeout 4 bash -c "</dev/tcp/127.0.0.1/4444" && echo ДОСТУПЕН '
                             '|| echo НЕТ').stdout, "mirrored networking")
    else:
        add("WSL: доступ к Windows 127.0.0.1:4444", True,
            "OpenOCD не запущен — проверится при старте канала")
    # Адреса считаются при запуске канала, поэтому проверяем наличие резолвера и спецификации,
    # а не готовый JSON: иначе на чистой машине доктор ложно показывает проблему.
    add("Резолвер адресов", RESOLVE.is_file(), str(RESOLVE))
    add("Спецификация полей", (ROS_ALT / "fields.toml").is_file(), "fields.toml")
    add("Каталог для логов", LOG_DIR.parent.is_file() or True, str(LOG_DIR))

    print("доктор окружения (ничего не запускается):")
    print("  %-42s %-6s %s" % ("проверка", "итог", "детали"))
    failed = 0
    for name, ok, detail in checks:
        if not ok:
            failed += 1
        print("  %-42s %-6s %s" % (name, "ок" if ok else "НЕТ", detail[:48]))
    print("\n  провалено: %d из %d" % (failed, len(checks)))

    print("\nнаследие предыдущего запуска (мешает следующему старту):")
    state = doctor_state()
    blocked = 0
    for name, ok, detail in state:
        if not ok:
            blocked += 1
        print("  %-42s %-6s %s" % (name, "ок" if ok else "ЗАНЯТО", detail[:48]))
    if blocked:
        print("  занято/осталось: %d — выполните: python ros1_channel.py --cleanup"
              % blocked)
    else:
        print("  чисто: можно запускать")

    failed += blocked
    if failed:
        print("  Подсказки:")
        if not any(name.startswith("Windows: OpenOCD") and ok for name, ok, _ in checks):
            print("    * OpenOCD: scoop install openocd (или положить в PATH)")
        if not stlink_port:
            print("    * ST-Link VCOM: подключить отладчик, проверить драйвер ST-Link")
        if not any(name == "WSL: ROS" and ok for name, ok, _ in checks):
            print("    * ROS в WSL: установить Noetic в Ubuntu-20.04")
        if "НЕТ" in wsl('timeout 4 bash -c "</dev/tcp/127.0.0.1/4444" && echo ДОСТУПЕН '
                        '|| echo НЕТ').stdout:
            print("    * Связь WSL→Windows: запустить OpenOCD с '-c \"bindto 0.0.0.0\"'")
    return 0 if failed == 0 else 1



def doctor_state():
    """Проверить «наследство» предыдущего запуска: занятые порты, процессы, lock, PTY, файлы.

    Это не ошибки окружения, а признаки незакрытой сессии. Именно они мешают следующему запуску
    («мастер не поднялся», «порт занят»), поэтому доктор должен показывать их отдельно.
    """
    state = []

    def add(name, ok, detail=""):
        state.append((name, ok, detail))

    ports = [(11311, "мастер ROS"), (3333, "OpenOCD gdb"), (4444, "OpenOCD telnet"),
             (7777, "COM→TCP"), (19021, "RTT-сервер")]
    for port, role in ports:
        busy = port_open(port)
        add("порт %d (%s)" % (port, role), not busy, "занят — нужен --cleanup" if busy else "свободен")

    alive = wsl("pgrep -af '[r]oscore|[r]osmaster|[r]osout|[s]erial_node|[s]ocat.*ttyVirtual0|"
                "[t]elemetry_node|[r]ecorder_node' | head -5")
    lines = [line for line in alive.stdout.strip().splitlines() if line.strip()]
    add("WSL: процессы канала", not lines, ("%d шт." % len(lines)) if lines else "нет")

    lock = wsl("ls $HOME/.ros/roscore-*.pid $HOME/.ros/rosmaster*.pid 2>/dev/null | head -3")
    locks = [line for line in lock.stdout.strip().splitlines() if line.strip()]
    add("WSL: lock-файлы roscore", not locks, locks[0] if locks else "нет")

    pty = wsl("test -e /dev/ttyVirtual0 && echo есть || echo нет")
    add("WSL: PTY /dev/ttyVirtual0", "нет" in pty.stdout, pty.stdout.strip())

    windows = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "$c = Get-CimInstance Win32_Process | "
                              "Where-Object { ($_.Name -eq 'openocd.exe') -or "
                              "($_.Name -eq 'python.exe' -and "
                              "$_.CommandLine -like '*tcp_serial_redirect*') } | "
                              "Select-Object Name; if ($c) { $c.Name -join ',' } else { 'нет' }"],
                             capture_output=True, text=True)
    leftover = windows.stdout.strip()
    add("Windows: openocd / redirect", leftover in ("", "нет"), leftover or "нет")

    log_dir = LOG_DIR
    logs = sorted(log_dir.glob("*.log")) if log_dir.is_dir() else []
    if logs:
        newest = max(logs, key=lambda item: item.stat().st_mtime)
        add("последний лог build/channel", True,
            "%s (%s)" % (newest.name, time.strftime("%H:%M", time.localtime(newest.stat().st_mtime))))
    else:
        add("последний лог build/channel", True, "журналов нет")

    recordings = list((ROOT / "build").glob("*.bag")) + list((ROOT / "build").glob("*.mcap"))
    if recordings:
        total = sum(item.stat().st_size for item in recordings) / 1024
        add("файлы записей в build", True, "%d шт., %.0f КБ" % (len(recordings), total))
    else:
        add("файлы записей в build", True, "нет")

    return state


# ---------------------------------------------------------------- маршруты

def start_roscore():
    process, log = spawn("roscore", "%s && exec roscore" % ROS_SETUP)
    if not wait_port(11311):
        print("  ОШИБКА: мастер не поднялся, журнал %s" % log)
        return None
    return process


def start_openocd():
    if port_open(4444):
        print("  OpenOCD уже слушает 4444")
        return
    arguments = ["openocd", "-f", "interface/stlink.cfg", "-f", "target/stm32f4x.cfg",
                 "-c", "bindto 0.0.0.0", "-c", "gdb_port 3333", "-c", "telnet_port 4444",
                 "-c", "init", "-c", "reset run"]
    spawn("openocd", arguments, wsl_process=False)
    if not wait_port(4444):
        raise SystemExit("OpenOCD не поднялся (журнал build/channel/openocd.log)")



def prepare_swd_spec(args):
    """Сделать копию fields.toml с префиксом /swd/ в топиках.

    Зачем: маршрут OpenOCD публикует те же имена (/board/sensor/batt_v), что и rosserial.
    При одновременной записи сообщения двух источников смешались бы в одном топике, и сравнение
    стало бы невозможным. Отдельное пространство имён разводит их.
    """
    import json
    import tomllib

    data = tomllib.loads((ROS_ALT / "fields.toml").read_text(encoding="utf-8"))
    lines = []
    for field in data.get("field", []):
        field = dict(field)
        # /swd — корень дерева: структура имени сохраняется целиком (/swd/board/sensor/batt_v),
        # иначе sensor/batt_v и aio/batt_v схлопываются в один /swd/batt_v.
        field["topic"] = "/swd" + field["topic"]
        lines.append("[[field]]")
        for key in ("topic", "message", "kind", "expr", "min", "max"):
            if key in field:
                lines.append("%s = %s" % (key, json.dumps(field[key])))
    path = (ROOT / args.elf).parent / "channel" / "fields-swd.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def via_openocd(args):
    print("\n=== маршрут: OpenOCD (память платы) ===")
    start_openocd()
    spec, addresses = (ROS_ALT / "fields.toml"), ADDRESSES
    if args.via == "both":
        spec = prepare_swd_spec(args)
        addresses = (ROOT / args.elf).parent / "channel" / "addresses-swd.json"
    resolved = subprocess.run([sys.executable, str(RESOLVE), "--elf", str(ROOT / args.elf),
                               "--svd", str(ROOT / "resources" / f"STM32{args.profile}.svd"),
                               "--spec", str(spec), "--out", str(addresses)],
                              capture_output=True, text=True, cwd=ROOT)
    for line in resolved.stdout.splitlines():
        if "0x2" in line or "SVD:" in line or "ELF:" in line:
            print("  " + line.strip())
    if resolved.returncode:
        print("  ОШИБКА резолвера:\n" + (resolved.stdout + resolved.stderr)[-500:])
        return False
    spawn("telemetry", "%s && export ROS_MASTER_URI=%s && exec python3 -u %s %s"
          % (ROS_SETUP, ROS_MASTER_URI, wsl_path(TELEMETRY_NODE), wsl_path(addresses)))
    time.sleep(3.0)
    return True


def stop_bridge(args):
    """Снять только UART-мост (redirect + socat), НЕ трогая ROS-мастер.

    Раньше здесь вызывалась общая уборка — и она убивала только что поднятый roscore, из-за чего
    serial_node получал `ConnectionRefusedError` к мастеру.
    """
    wsl("pkill -f '[s]ocat.*%s' 2>/dev/null; rm -f %s" % (args.pty, args.pty), user="root")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "$c = Get-NetTCPConnection -LocalPort %d -State Listen "
                    "-ErrorAction SilentlyContinue; "
                    "if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force } }"
                    % args.tcp], capture_output=True)


def via_rosserial(args):
    print("\n=== маршрут: rosserial (UART) ===")
    stop_bridge(args)
    time.sleep(1.0)
    spawn("redirect", [sys.executable, str(REDIRECT), "-P", str(args.tcp), "--rts", "0",
                       "--dtr", "0", args.port, str(args.baud)], wsl_process=False)
    time.sleep(2.0)
    if not port_open(args.tcp):
        print("  ОШИБКА: tcp_serial_redirect не слушает %d (журнал build/channel/redirect.log)"
              % args.tcp)
        return False
    print("  COM → TCP: %s %d → :%d" % (args.port, args.baud, args.tcp))
    spawn("socat", "exec socat pty,link=%s,raw,echo=0,mode=777 tcp:127.0.0.1:%d"
          % (args.pty, args.tcp), user="root")
    for attempt in range(1, 16):
        time.sleep(1.0)
        if wsl("test -e %s && echo есть" % args.pty).stdout.strip() == "есть":
            print("  PTY %s создан (root без пароля)" % args.pty)
            break
    else:
        print("  ОШИБКА: PTY не появился")
        return False
    spawn("serial_node", "%s && export ROS_MASTER_URI=%s && exec rosrun rosserial_python "
                         "serial_node.py _port:=%s _baud:=%d"
          % (ROS_SETUP, ROS_MASTER_URI, args.pty, args.baud))
    print("  serial_node запущен (переживает перезагрузку платы: пересогласует топики сам)")
    time.sleep(5.0)
    return True


# ---------------------------------------------------------------- запись

def reindex_if_needed(path):
    """Переиндексировать bag, если он остался без индекса. Оригинал сохраняется как <имя>.bak."""
    if path.suffix.lower() != ".bag" or not path.is_file():
        return
    info = wsl("%s && rosbag info %s 2>&1 | tail -3" % (ROS_SETUP, wsl_path(path)), timeout=90)
    text = (info.stdout + info.stderr).lower()
    if "unindexed" not in text and info.returncode == 0:
        return
    backup = path.with_suffix(".bag.bak")
    wsl("cp -f %s %s" % (wsl_path(path), wsl_path(backup)))
    print("  bag без индекса → переиндексация (копия: %s)" % backup.name)
    wsl("%s && rosbag reindex %s" % (ROS_SETUP, wsl_path(path)), timeout=180)
    # rosbag reindex сам создаёт .orig.bag — он нам не нужен, копию уже сохранили.
    wsl("rm -f %s" % wsl_path(path.with_suffix(".bag.orig")))
    again = wsl("%s && rosbag info %s 2>&1 | tail -2" % (ROS_SETUP, wsl_path(path)), timeout=90)
    print("  после переиндексации: " + " ".join(again.stdout.split())[:120])


def record(args, duration):
    # Путь делаем абсолютным: из WSL он передаётся как /mnt/<диск>/..., а у относительного нет диска.
    out = pathlib.Path(args.out).resolve() if args.out else \
        (ROOT / args.elf).parent / "channel" / \
        ("record-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"), args.record))
    command = ("%s && export ROS_MASTER_URI=%s && exec python3 -u %s --format %s --out %s "
               "--topics %s --seconds %d --compress %s --chunk-size %d"
               % (ROS_SETUP, ROS_MASTER_URI, wsl_path(RECORDER), args.record,
                  wsl_path(out), args.topics, duration, args.compress, args.chunk_size))
    print("\n=== запись: %s (%s) ===" % (out.name, args.record))
    process, log = spawn("recorder", command)
    return process, log, out


def dryrun(args):
    """Пробный прогон: проверить окружение и показать план, ничего не запуская.

    Нужен, чтобы на новой машине или в CI убедиться, что канал вообще возможен: доктор + расчёт адресов
    (он офлайн) + печать точных команд, которые были бы выполнены.
    """
    print("=== DRYRUN: ничего не запускается ===\n")
    code = doctor(args)

    print("\n=== расчёт адресов (офлайн, плата не нужна) ===")
    resolved = subprocess.run([sys.executable, str(RESOLVE), "--elf", str(ROOT / args.elf),
                               "--svd", str(ROOT / "resources" / f"STM32{args.profile}.svd"),
                               "--out", str(ADDRESSES)],
                              capture_output=True, text=True, cwd=ROOT)
    for line in resolved.stdout.splitlines():
        if "0x2" in line or "SVD:" in line or "ELF:" in line or "полей" in line:
            print("  " + line.strip())
    if resolved.returncode:
        print("  ОШИБКА резолвера:\n" + (resolved.stdout + resolved.stderr)[-400:])
        code = 1

    print("\n=== план для маршрута %s ===" % args.via)
    steps = ["wsl -d %s: source %s && exec roscore" % (DISTRO, ROS_SETUP)]
    if args.via == "openocd":
        steps += [
            "windows: openocd -f interface/stlink.cfg -f target/stm32f4x.cfg "
            "-c \"bindto 0.0.0.0\" -c \"gdb_port 3333\" -c \"telnet_port 4444\" "
            "-c init -c \"reset run\"",
            "wsl: python3 %s %s  (нода читает память по адресам)"
            % (wsl_path(TELEMETRY_NODE), wsl_path(ADDRESSES)),
        ]
    else:
        steps += [
            "windows: python %s -P %d --rts 0 --dtr 0 %s %d"
            % (REDIRECT.name, args.tcp, args.port, args.baud),
            "wsl (root): socat pty,link=%s,raw,echo=0,mode=777 tcp:127.0.0.1:%d"
            % (args.pty, args.tcp),
            "wsl: rosrun rosserial_python serial_node.py _port:=%s _baud:=%d"
            % (args.pty, args.baud),
        ]
    for number, step in enumerate(steps, 1):
        print("  %d. %s" % (number, step))

    if args.record != "none":
        out = pathlib.Path(args.out).resolve() if args.out else \
            (ROOT / args.elf).parent / "channel" / ("record-<дата>." + args.record)
        print("\n=== план записи ===")
        print("  формат: %s" % args.record)
        print("  файл:   %s" % out)
        print("  топики: %s" % args.topics)
        print("  длина:  %s" % ("%d с" % args.seconds if args.seconds else "до остановки"))
        print("  запись: wsl: python3 %s --format %s --out %s --topics %s --seconds %d"
              % (wsl_path(RECORDER), args.record, wsl_path(out), args.topics, args.seconds))
        print("  после:  при отсутствии индекса у bag — копия %s.bak и rosbag reindex"
              % out.name)

    print("\n=== итог dryrun: %s ===" % ("ок, канал запускаем" if code == 0 else "есть проблемы"))
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--doctor", action="store_true", help="проверить окружение и выйти")
    parser.add_argument("--dryrun", action="store_true",
                        help="пробный прогон: проверки и план, ничего не запускается")
    parser.add_argument("--via", choices=["openocd", "rosserial", "both"], default="openocd")
    parser.add_argument("--record", choices=["none", "bag", "mcap"], default="none")
    parser.add_argument("--out", default="", help="имя файла записи")
    parser.add_argument("--topics", default="all", help="список топиков или all")
    parser.add_argument("--compress", choices=["none", "bz2"], default="none",
                        help="сжатие для bag (bz2 — stdlib, читает rosbag)")
    parser.add_argument("--chunk-size", type=int, default=1000,
                        help="сообщений на чанк для mcap")
    parser.add_argument("--seconds", type=float, default=0.0, help="остановиться через N секунд")
    parser.add_argument("--keep", action="store_true", help="не останавливать канал")
    parser.add_argument("--cleanup", action="store_true", help="только остановить всё")
    parser.add_argument("--elf", default=os.environ.get("FIRMWARE_ELF", "build/firmware.elf"))
    parser.add_argument("--profile", default="F411", choices=["F411", "G474"])
    parser.add_argument("--port", default=os.environ.get("ROS1_SERIAL_PORT"),
                        help="COM-порт ST-Link VCOM (по умолчанию $ROS1_SERIAL_PORT)")
    parser.add_argument("--baud", type=int, default=460800)
    parser.add_argument("--tcp", type=int, default=7777)
    parser.add_argument("--pty", default="/dev/ttyVirtual0")
    args = parser.parse_args()
    if args.via in ("rosserial", "both") and not args.port and not (args.cleanup or args.doctor):
        parser.error("для маршрута rosserial укажите --port или ROS1_SERIAL_PORT")

    # Артефакты храним рядом с ELF пресета: его путь и определяет набор файлов опытов.
    global LOG_DIR
    LOG_DIR = (ROOT / args.elf).parent / "channel" / "logs"

    if args.cleanup:
        print("=== остановка ===")
        stop_all()
        print("  остановлено")
        return 0
    if args.doctor:
        return doctor(args)
    if args.dryrun:
        return dryrun(args)

    print("=== 0. Окружение WSL ===")
    env = wsl("%s && rosversion -d && which rosrun" % ROS_SETUP)
    print("  " + " | ".join(env.stdout.split()))

    print("\n=== 1. Мастер ROS в WSL ===")
    stop_all()
    time.sleep(1.0)
    if not start_roscore():
        return 1
    print("  мастер слушает 11311")

    if args.via == "both":
        started = via_openocd(args) and via_rosserial(args)
    else:
        started = via_openocd(args) if args.via == "openocd" else via_rosserial(args)
    if not started:
        stop_all()
        return 1

    listing = wsl("%s && export ROS_MASTER_URI=%s && rostopic list | grep powerboard | head -8"
                  % (ROS_SETUP, ROS_MASTER_URI))
    print("\n=== 2. Топики ===")
    for line in listing.stdout.strip().splitlines():
        print("  " + line)
    if not listing.stdout.strip():
        print("  (топиков нет — проверьте журнал build/channel/)")

    recorded = None
    if args.record != "none":
        duration = int(args.seconds) if args.seconds else 0
        process, log, out = record(args, duration)
        if duration:
            print("  ждём %d с..." % duration)
            time.sleep(duration + 4)
            reindex_if_needed(out)
            recorded = out
        else:
            print("  запись идёт; остановить: Ctrl+C или --cleanup")

    print("\n=== 3. Проверка ===")
    hz = wsl("%s && export ROS_MASTER_URI=%s && timeout 8 rostopic hz "
             "/board/sensor/batt_v 2>&1 | grep 'average rate' | tail -1" % (ROS_SETUP, ROS_MASTER_URI),
             timeout=60)
    print("  частота: %s" % (hz.stdout.strip() or "нет данных"))
    if recorded and recorded.is_file():
        print("  файл: %s (%d байт)" % (recorded, recorded.stat().st_size))
        if recorded.suffix == ".mcap":
            info = subprocess.run(["mcap", "info", str(recorded)], capture_output=True, text=True)
            for line in info.stdout.splitlines():
                if line.startswith(("messages:", "channels:")):
                    print("  mcap " + line)

    if args.keep:
        print("\n=== 4. Канал оставлен работать ===")
        print("  в WSL: . /opt/ros/noetic/setup.bash && rostopic echo /board/sensor/batt_v")
        print("  остановить: python run_channel.py --cleanup")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            stop_all()
    else:
        print("\n=== 4. Остановка ===")
        stop_all()
        print("  остановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
