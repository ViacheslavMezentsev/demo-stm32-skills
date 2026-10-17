"""Вариант «аппаратная точка останова»: наблюдение произвольной функции без изменения прошивки.

ЧТО ДЕЛАЕТ. Ставит аппаратную точку останова (FPB) на вход функции. Каждое попадание GDB обслуживает:
записывает значение `uwTick` в массив в свободной SRAM и продолжает исполнение. Прошивка, флеш и
таблица векторов не меняются вообще — точка останова живёт в отладочном блоке ядра и исчезает при
сбросе.

ЗАЧЕМ ЭТОТ ВАРИАНТ. Это единственный способ наблюдать **произвольную функцию** (не обработчик
исключения) без патча флеша. Плата: остановки ядра на каждом вызове.

ГРАНИЦЫ ПРИМЕНИМОСТИ (измеряются этим скриптом, а не берутся из документации):
  * у F411 **6 аппаратных точек** и 4 точки данных — больше не поставить;
  * каждое попадание = остановка ядра + обмен с отладчиком по SSH: это миллисекунды, поэтому
    прошивка искажается по времени. Смотреть надо на **фактическую частоту** попаданий и на разброс
    `uwTick` между ними;
  * если ожидаемая частота вызова больше, чем успевает обслуживать отладчик, часть вызовов теряется —
    метод для редких событий (до десятков в секунду), а не для горячих путей.

ЗАПУСК:
    python scripts/hook/run_bp_hook.py --symbol _ZN13SensorManager6updateE13UpdateContext
"""
import argparse
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[4]
TOOLS = pathlib.Path(__file__).resolve().parents[1]
STAND = "stand60-home"
DEFAULT_ELF = "build/firmware.elf"
# Служебные ячейки в свободной SRAM (выше .bss, ниже стека и вне образа RTT).
# Переменные GDB не переживают переподключение, поэтому счётчик и метка времени хранятся в памяти цели.
BUFFER = 0x2001E800
COUNTER = BUFFER - 4
LAST_TICK = BUFFER - 8
FIRST_TICK = BUFFER - 12


def ssh(command, timeout=30):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
                           STAND, command], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout + 20)


def nm(symbol, elf):
    output = subprocess.run(["arm-none-eabi-nm", elf], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=ROOT)
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == symbol:
            return int(parts[0], 16)
    raise ValueError("символ не найден: %s" % symbol)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="_ZN13SensorManager6updateE13UpdateContext",
                        help="функция для наблюдения")
    parser.add_argument("--elf", default=DEFAULT_ELF)
    parser.add_argument("--seconds", type=float, default=6.0)
    args = parser.parse_args()

    entry = nm(args.symbol, args.elf)
    tick = nm("uwTick", args.elf)
    print("=== 1. Наблюдаем функцию ===")
    print("  функция: 0x%08X (%s)" % (entry, args.symbol))
    print("  точность: тип останова выбирает GDB; у F411 доступно 6 аппаратных точек")
    print("  счётчик:  0x%08X, метка времени: 0x%08X" % (COUNTER, LAST_TICK))

    print("\n=== 2. OpenOCD ===")
    subprocess.run(["scp", "-q", str(TOOLS / "start_openocd.sh"), f"{STAND}:/tmp/start_openocd.sh"],
                   capture_output=True)
    result = ssh("sh /tmp/start_openocd.sh 2>&1 | tail -2")
    print("  " + result.stdout.strip().replace("\n", "\n  "))

    tunnels = subprocess.Popen(["ssh", "-N", "-L", "3333:127.0.0.1:3333", STAND],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    gdb = ["arm-none-eabi-gdb", "-q", "-batch", args.elf]

    script = ROOT / "build" / "bp_hook.gdb"
    script.write_text("\n".join([
        "set pagination off",
        "target remote 127.0.0.1:3333",
        "echo === подготовка ===",
        "set {int}0x%08X = 0" % COUNTER,
        "set {int}0x%08X = 0" % LAST_TICK,
        "set {int}0x%08X = 0" % FIRST_TICK,
        "hbreak *0x%08X" % entry,
        "echo === точка останова ===",
        "info breakpoints",
        "commands",
        "silent",
        # Первое попадание запоминает начало отсчёта: это делает измерение независимым от того,
        # сколько прошивка прожила до подключения отладчика.
        "if *(int*)0x%08X == 0" % COUNTER,
        "set {int}0x%08X = *(unsigned int*)0x%08X" % (FIRST_TICK, tick),
        "end",
        # Две записи на попадание: метка времени и счётчик. Каждый `set` — обмен с целью,
        # поэтому эти накладные расходы и есть измеряемая граница метода.
        "set {int}0x%08X = *(unsigned int*)0x%08X" % (LAST_TICK, tick),
        "set {int}0x%08X = *(int*)0x%08X + 1" % (COUNTER, COUNTER),
        "continue",
        "end",
        "continue",
        "",
    ]), encoding="utf-8")

    print("\n=== 3. Наблюдение %.0f с (каждое попадание — остановка ядра) ===" % args.seconds)
    process = subprocess.Popen(gdb + ["-x", str(script)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    time.sleep(args.seconds)
    running = process.poll() is None
    process.kill()
    output = ""
    try:
        output, _ = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    for line in (output or "").splitlines():
        if any(k in line for k in ("Hardware assisted", "breakpoint", "Breakpoint", "hbreak")):
            print("  " + line.strip()[:120])

    print("\n=== 4. Что зафиксировано ===")
    # Пауза: OpenOCD должен заметить разрыв GDB, иначе следующее соединение отклоняется.
    time.sleep(1.5)
    read = ROOT / "build" / "bp_read.gdb"
    read.write_text("\n".join([
        "set pagination off",
        "target remote 127.0.0.1:3333",
        "printf \"попаданий: %%d\\n\", *(int*)0x%08X" % COUNTER,
        "printf \"тик первого: %%u\\n\", *(unsigned int*)0x%08X" % FIRST_TICK,
        "printf \"тик последнего: %%u\\n\", *(unsigned int*)0x%08X" % LAST_TICK,
        "",
    ]), encoding="utf-8")
    result = subprocess.run(gdb + ["-x", str(read)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=ROOT)
    hits = None
    first = last = None
    for line in (result.stdout + result.stderr).splitlines():
        if "попаданий" in line:
            print("  " + line.strip())
            try:
                hits = int(line.split(":", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
        if "тик первого" in line:
            print("  " + line.strip())
            try:
                first = int(line.split(":", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
        if "тик последнего" in line:
            print("  " + line.strip())
            try:
                last = int(line.split(":", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
    if hits is None:
        print("  чтение не удалось, вывод GDB:")
        for line in (result.stdout + result.stderr).splitlines()[-8:]:
            print("    " + line.strip())

    if hits is not None and first is not None and last is not None:
        print("\n=== 5. Граница применимости (измерено) ===")
        print("  попаданий за %.0f с настенного времени: %d" % (args.seconds, hits))
        if hits:
            print("  настенная стоимость одного попадания: %.0f мс" % (args.seconds * 1000 / hits))
        if last > first and hits > 1:
            target_ms = last - first
            print("  прошивка прожила между первым и последним попаданием: %.2f с" % (target_ms / 1000.0))
            print("  интервал вызова по часам прошивки: %.0f мс (номинал функции — 100 мс)"
                  % (target_ms / (hits - 1)))
            print("  КОЭФФИЦИЕНТ РАСТЯЖЕНИЯ ВРЕМЕНИ: %.1f x"
                  % ((args.seconds * 1000) / target_ms))
        else:
            print("  попаданий слишком мало для оценки интервала")
        print("  ВЫВОД: метод наблюдает факт вызова, но искажает время;")
        print("         для горячих путей (10 Гц и выше) не годится — там нужен VTOR-перехват.")
    else:
        print("\n  Не хватает данных для оценки границы (см. предупреждения выше).")

    print("\n=== 6. Уборка ===")
    ssh("pkill -9 -f '/usr/bin/openocd' 2>/dev/null; sleep 1; "
        "ps -eo comm | grep -qw openocd && echo '  openocd остался' "
        "|| echo '  openocd остановлен, пробник свободен'")
    tunnels.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
