"""Нативный перехват: свободный таймер TIM2 + копия таблицы векторов в SRAM.

ЧТО ДЕЛАЕТ. Направляет прерывание **неиспользуемого** таймера TIM2 на заглушку в SRAM и включает его.
Заглушка выполняется на самой цели с полной скоростью и пишет кадры в кольцевой буфер RTT. Отладчик
нужен только чтобы всё это установить — в работе канала он не участвует.

ПОЧЕМУ ЭТО ЛУЧШЕ ОСТАЛЬНЫХ ВАРИАНТОВ.
  * Точка останова и перехват возврата стоят ~1 с на событие (обслуживает хост) — не для потока.
  * VTOR на SysTick даёт 1 кГц, но трогает часы HAL и фиксирован по частоте.
  * Здесь: частота **задаётся нами** (Гц…десятки кГц), HAL не затрагивается, прошивка о таймере не знает.
  * Флеш не меняется: копия таблицы векторов в SRAM + `SCB->VTOR`.

ЧТО ИМЕННО ПРОВЕРЯЕТСЯ. Фактическая частота по **часам прошивки** (`uwTick` в каждом кадре), а не по
настенному времени: так измерение не зависит от хоста.

ТРЕБОВАНИЯ К ПЛАТЕ (проверены для текущий пресет):
  * TIM2 не используется прошивкой (0 упоминаний в исходниках);
  * вектор TIM2 (исключение 44) ведёт на `Default_Handler` — его и подменяем в КОПИИ таблицы;
  * в NVIC включено только `RTC_Alarm`, поэтому IRQ 28 свободно.

ЗАПУСК:
    python scripts/hook/run_timer_hook.py --hz 1000 --seconds 5
"""
import argparse
import pathlib
import socket
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path.cwd()  # корень проекта прошивки: запускать из него (build/firmware.elf)
TOOLS = pathlib.Path(__file__).resolve().parent.parent
STAND = "stand60-home"
DEFAULT_ELF = "build/firmware.elf"

RTT_CB = 0x2001E000
RTT_BUFFER = 0x2001E100
RTT_SIZE = 1024
RTT_WROFF = 36
VECTORS = 0x2001F000
STUB = 0x2001F400
# Счётчик прерываний: заглушка увеличивает его на каждое прерывание, поэтому частота измеряется
# независимо от переполнения кольцевого буфера.
COUNTER = 0x2001F7F0
FIRST_TICK = 0x2001F7F4
LAST_TICK = 0x2001F7F8
RTT_PORT = 19021
VTOR_ADDRESS = 0xE000ED08

# TIM2 (STM32F411).
TIM2_IRQ = 28
TIM2_EXCEPTION = 16 + TIM2_IRQ          # 44
TIM2_BASE = 0x40000000
TIM2_CR1 = TIM2_BASE + 0x00
TIM2_DIER = TIM2_BASE + 0x0C
TIM2_SR = TIM2_BASE + 0x10
TIM2_EGR = TIM2_BASE + 0x14
TIM2_PSC = TIM2_BASE + 0x28
TIM2_ARR = TIM2_BASE + 0x2C
RCC_APB1ENR = 0x40023840
RCC_CFGR = 0x40023808
NVIC_ISER0 = 0xE000E100


def ssh(command, timeout=30):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
                           STAND, command], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout + 20)


def telnet(command, port=4444, timeout=4):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall((command + "\n").encode())
            sock.settimeout(1.0)
            chunks = []
            while True:
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks).decode("utf-8", "replace")
    except OSError as error:
        return "ОШИБКА: %s" % error


def nm(symbol, elf):
    output = subprocess.run(["arm-none-eabi-nm", elf], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=ROOT)
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == symbol:
            return int(parts[0], 16)
    raise ValueError("символ не найден: %s" % symbol)


def vector_table(elf):
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as handle:
        temporary = pathlib.Path(handle.name)
    try:
        result = subprocess.run(["arm-none-eabi-objcopy", "-O", "binary",
                                 "--only-section=.isr_vector", elf, str(temporary)],
                                capture_output=True, cwd=ROOT)
        if result.returncode or not temporary.is_file():
            raise ValueError("не удалось прочитать .isr_vector")
        data = temporary.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
    return [struct.unpack_from("<I", data, offset)[0] for offset in range(0, len(data), 4)]


def build_stub(tick_var):
    result = subprocess.run(
        ["arm-none-eabi-as", "-mcpu=cortex-m4", "-mthumb",
         "--defsym", "RTT_CB=0x%X" % RTT_CB, "--defsym", "RTT_WROFF=%d" % RTT_WROFF,
         "--defsym", "RTT_BUF=0x%X" % RTT_BUFFER, "--defsym", "RTT_SIZE=%d" % RTT_SIZE,
         "--defsym", "TICK_VAR=0x%X" % tick_var, "--defsym", "TIM_SR=0x%X" % TIM2_SR,
         "--defsym", "COUNTER=0x%X" % COUNTER,
         "--defsym", "FIRST_TICK=0x%X" % FIRST_TICK,
         "--defsym", "LAST_TICK=0x%X" % LAST_TICK,
         "-o", str(ROOT / "build" / "timer_hook.o"), str(TOOLS / "hook" / "timer_hook.S")],
        capture_output=True, text=True, cwd=ROOT)
    if result.returncode:
        raise ValueError("ассемблирование: %s" % result.stderr[:300])
    out = ROOT / "build" / "timer_hook.bin"
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary",
                    str(ROOT / "build" / "timer_hook.o"), str(out)], check=True, cwd=ROOT)
    return out.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hz", type=int, default=1000, help="желаемая частота прерываний")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--no-rtt", action="store_true",
                        help="не поднимать RTT: только измерить частоту по счётчику (чтение RTT "
                             "через OpenOCD приостанавливает ядро и искажает замер)")
    parser.add_argument("--elf", default=DEFAULT_ELF)
    args = parser.parse_args()

    tick = nm("uwTick", args.elf)
    table = vector_table(args.elf)
    print("=== 1. Что используем ===")
    print("  uwTick: 0x%08X" % tick)
    print("  вектор TIM2: исключение %d, сейчас 0x%08X (Default_Handler)"
          % (TIM2_EXCEPTION, table[TIM2_EXCEPTION]))
    print("  заглушка: 0x%08X, копия таблицы: 0x%08X" % (STUB, VECTORS))

    stub = build_stub(tick)
    print("  заглушка: %d байт" % len(stub))

    size = STUB + 0x200 - RTT_CB
    image = bytearray(size)
    block = bytearray(48)
    block[0:16] = b"SEGGER RTT" + b"\0" * 6
    struct.pack_into("<i", block, 16, 1)
    struct.pack_into("<i", block, 20, 0)
    struct.pack_into("<I", block, 24 + 4, RTT_BUFFER)
    struct.pack_into("<I", block, 24 + 8, RTT_SIZE)
    image[0:48] = block
    vectors = list(table)
    vectors[TIM2_EXCEPTION] = STUB | 1
    table_bytes = b"".join(struct.pack("<I", word) for word in vectors)
    image[VECTORS - RTT_CB:VECTORS - RTT_CB + len(table_bytes)] = table_bytes
    image[STUB - RTT_CB:STUB - RTT_CB + len(stub)] = stub
    image_path = ROOT / "build" / "timer-hook-image.bin"
    image_path.write_bytes(bytes(image))
    print("  образ: %s (%d байт)" % (image_path.name, len(image)))

    print("\n=== 2. OpenOCD ===")
    subprocess.run(["scp", "-q", str(TOOLS / "start_openocd.sh"), f"{STAND}:/tmp/start_openocd.sh"],
                   capture_output=True)
    result = ssh("sh /tmp/start_openocd.sh 2>&1 | tail -2")
    print("  " + result.stdout.strip().replace("\n", "\n  "))

    tunnels = subprocess.Popen(
        ["ssh", "-N", "-L", "3333:127.0.0.1:3333", "-L", "4444:127.0.0.1:4444",
         "-L", f"{RTT_PORT}:127.0.0.1:{RTT_PORT}", STAND],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    gdb = ["arm-none-eabi-gdb", "-q", "-batch", args.elf]

    # Сначала узнаём часы: частота таймера на APB1 равна PCLK1 (если делитель 1) или 2*PCLK1.
    probe = ROOT / "build" / "timer_probe.gdb"
    probe.write_text("\n".join([
        "set pagination off",
        "target remote 127.0.0.1:3333",
        "printf \"RCC_CFGR: 0x%%08x\\n\", *(unsigned int*)0x%08X" % RCC_CFGR,
        "printf \"SystemCoreClock: %%u\\n\", *(unsigned int*)0x%08X" % nm("SystemCoreClock", args.elf),
        "",
    ]), encoding="utf-8")
    result = subprocess.run(gdb + ["-x", str(probe)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=ROOT)
    time.sleep(1.5)
    cfgr = core = None
    for line in (result.stdout + result.stderr).splitlines():
        if "RCC_CFGR" in line:
            cfgr = int(line.split(":")[1].strip().split()[0], 16)
        if "SystemCoreClock" in line:
            core = int(line.split(":")[1].strip().split()[0])
    if cfgr is None or core is None:
        print("  не удалось прочитать тактирование, вывод:")
        print("  " + (result.stdout + result.stderr)[-300:])
        return 1
    ppre1 = (cfgr >> 10) & 0x7
    divider = (1, 2, 4, 8, 16, 64, 128, 256)[ppre1] if ppre1 < 8 else 1
    pclk1 = core // divider if divider else core
    timer_clock = pclk1 * 2 if divider != 1 else pclk1
    print("\n=== 3. Тактирование ===")
    print("  SystemCoreClock: %d Гц, PPRE1: /%d, PCLK1: %d Гц" % (core, divider, pclk1))
    print("  такт таймера на APB1: %d Гц" % timer_clock)

    # Делитель подбираем так, чтобы ARR попадал в 16 бит.
    prescaler = 0
    while timer_clock // (prescaler + 1) > args.hz * 65536:
        prescaler += 1
    period = timer_clock // (prescaler + 1) // args.hz - 1
    actual = timer_clock // (prescaler + 1) / (period + 1)
    print("  PSC=%d, ARR=%d -> ожидаемая частота %.1f Гц" % (prescaler, period, actual))

    setup = ROOT / "build" / "timer_setup.gdb"
    setup.write_text("\n".join([
        "set pagination off",
        "target remote 127.0.0.1:3333",
        "echo === заливка образа ===",
        "restore %s binary 0x%08X" % (image_path.as_posix(), RTT_CB),
        "echo === копия таблицы и VTOR ===",
        "set {int}0x%08X = 0" % COUNTER,
        "set {int}0x%08X = 0" % FIRST_TICK,
        "set {int}0x%08X = 0" % LAST_TICK,
        "printf \"тик до: %%u\\n\", *(unsigned int*)0x%08X" % tick,
        "set *(unsigned int*)0x%08X = 0x%08X" % (VTOR_ADDRESS, VECTORS),
        "printf \"VTOR: 0x%%08x\\n\", *(unsigned int*)0x%08X" % VTOR_ADDRESS,
        "printf \"вектор TIM2 в копии: 0x%%08x\\n\", *(unsigned int*)(0x%08X + %d)"
        % (VECTORS, TIM2_EXCEPTION * 4),
        "echo === тактирование TIM2 ===",
        "set *(unsigned int*)0x%08X = *(unsigned int*)0x%08X | 1" % (RCC_APB1ENR, RCC_APB1ENR),
        "set *(unsigned int*)0x%08X = 0" % TIM2_CR1,
        "set *(unsigned int*)0x%08X = %d" % (TIM2_PSC, prescaler),
        "set *(unsigned int*)0x%08X = %d" % (TIM2_ARR, period),
        "set *(unsigned int*)0x%08X = 1" % TIM2_EGR,
        "set *(unsigned int*)0x%08X = 1" % TIM2_DIER,
        "set *(unsigned int*)0x%08X = *(unsigned int*)0x%08X | (1 << %d)"
        % (NVIC_ISER0, NVIC_ISER0, TIM2_IRQ),
        "printf \"TIM2 CR1/DIER/ARR: 0x%%08x / 0x%%08x / %%u\\n\", "
        "*(unsigned int*)0x%08X, *(unsigned int*)0x%08X, *(unsigned int*)0x%08X"
        % (TIM2_CR1, TIM2_DIER, TIM2_ARR),
        "printf \"NVIC ISER0: 0x%%08x\\n\", *(unsigned int*)0x%08X" % NVIC_ISER0,
        "echo === запуск таймера ===",
        "set *(unsigned int*)0x%08X = 1" % TIM2_CR1,
        "",
    ]), encoding="utf-8")

    try:
        print("\n=== 4. Установка нативного перехвата ===")
        # Пауза после сессии-пробы: OpenOCD должен заметить разрыв GDB, иначе следующее соединение
        # отклоняется («Error in sourced command file» на `target remote`).
        time.sleep(1.5)
        setup_output = ""
        for attempt in range(1, 4):
            result = subprocess.run(gdb + ["-x", str(setup)], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", cwd=ROOT)
            setup_output = result.stdout + result.stderr
            if "VTOR:" in setup_output and "Error in sourced" not in setup_output:
                break
            print("  попытка %d не удалась, повтор" % attempt)
            time.sleep(2.0)
        for line in setup_output.splitlines():
            if any(k in line for k in ("VTOR", "вектор", "TIM2", "NVIC", "тик до", "Restoring", "Error")):
                print("  " + line.strip())

        if not args.no_rtt:
            for command in ('rtt setup 0x%X 0x1000 "SEGGER RTT"' % RTT_CB, "rtt start",
                            "rtt server start %d 0" % RTT_PORT):
                answer = telnet(command)
                if "found" in answer:
                    print("  Control block found")

        print("\n=== 5. Прошивка работает, читаем поток ===")
        # Пауза перед третьим соединением: OpenOCD должен освободить предыдущее, иначе `continue`
        # не выполнится и цель останется стоять.
        time.sleep(2.0)
        process = subprocess.Popen(gdb + ["-ex", "target remote 127.0.0.1:3333",
                                          "-ex", "continue"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
        time.sleep(1.5)
        data = b""
        try:
            with socket.create_connection(("127.0.0.1", RTT_PORT), timeout=10) as sock:
                sock.settimeout(2.0)
                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline:
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    data += chunk
        except OSError as error:
            print("  ошибка чтения: %s" % error)
        running = process.poll() is None
        process.kill()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        print("  цель работала всё время: %s" % ("да" if running else "нет"))

        print("\n=== 6. Результат ===")
        # Частоту считаем по СЧЁТЧИКУ прерываний и часам прошивки: кольцевой буфер на высокой частоте
        # перезаписывается, поэтому кадры поток занижают.
        read = ROOT / "build" / "timer_read.gdb"
        read.write_text("\n".join([
            "set pagination off",
            "target remote 127.0.0.1:3333",
            "printf \"прерываний: %%u\\n\", *(unsigned int*)0x%08X" % COUNTER,
            "printf \"тик первого: %%u\\n\", *(unsigned int*)0x%08X" % FIRST_TICK,
            "printf \"тик последнего: %%u\\n\", *(unsigned int*)0x%08X" % LAST_TICK,
            "printf \"тик после: %%u\\n\", *(unsigned int*)0x%08X" % tick,
            "",
        ]), encoding="utf-8")
        time.sleep(1.5)
        result = subprocess.run(gdb + ["-x", str(read)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", cwd=ROOT)
        interrupts = first = last = None
        for line in (result.stdout + result.stderr).splitlines():
            if "прерываний" in line:
                print("  " + line.strip())
                try:
                    interrupts = int(line.split(":", 1)[1].strip().split()[0])
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
            if "тик после" in line:
                print("  " + line.strip())

        frames = [data[i:i + 4] for i in range(0, len(data) - 3, 4)]
        samples = [f for f in frames if f and f[0] == 0x4D]
        print("  кадров в потоке: %d, байт: %d" % (len(samples), len(data)))
        if interrupts is not None and first is not None and last is not None and last > first:
            elapsed = (last - first) / 1000.0
            print("  окно по часам прошивки: %.2f с (от первого до последнего прерывания)" % elapsed)
            print("  прерываний: %d" % interrupts)
            print("  ФАКТИЧЕСКАЯ ЧАСТОТА (по счётчику и часам прошивки): %.0f Гц"
                  % (interrupts / elapsed))
            print("  задано: %d Гц" % args.hz)
        else:
            print("  частота не определена: нет двух границ окна (first=%s, last=%s)"
                  % (first, last))
        if samples:
            ticks = [f[1] | (f[2] << 8) | (f[3] << 16) for f in samples]
            print("  тики в кадрах: %s ... %s" % (ticks[:4], ticks[-3:]))
            print("  НАТИВНОЕ ИСПОЛНЕНИЕ: заглушка работает по таймеру, отладчик в цепочке не участвует")
        elif not args.no_rtt:
            print("  кадров нет — проверьте настройку TIM2 и вектор")
    finally:
        tunnels.terminate()
        ssh("pkill -9 -f '/usr/bin/openocd' 2>/dev/null; sleep 1; "
            "ps -eo comm | grep -qw openocd && echo '  openocd остался' "
            "|| echo '  openocd остановлен, пробник свободен'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
