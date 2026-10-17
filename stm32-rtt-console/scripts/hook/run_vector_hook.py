"""Типовое применение: прозрачный перехват вектора исключения с записью в RTT.

ЧТО ДЕЛАЕТ. Копирует таблицу векторов прошивки в свободную SRAM, подменяет в копии ОДИН вектор на
заглушку и переключает `SCB->VTOR` на копию. Заглушка пишет в кольцевой буфер RTT кадр наблюдения и
**вызывает штатный обработчик**, поэтому поведение прошивки не меняется — перехват только наблюдает.

ПОЧЕМУ ФЛЕШ НЕ ТРОГАЕТСЯ. `VTOR` — штатный механизм Cortex-M для переноса таблицы векторов. Код
прошивки исполняется по прежним адресам, ни один байт флеша не пишется.

ЧЕМ ОТЛИЧАЕТСЯ ОТ ПРЕДЫДУЩЕЙ ВЕРСИИ. Первая заглушка выходила из прерывания, не вызывая прежний
обработчик, и `HAL_IncTick` не выполнялся — `HAL_Delay` ломался. Здесь штатный обработчик вызывается
через `blx`, поэтому наблюдение прозрачно.

КАК ПРОВЕРЯЕТСЯ ПРОЗРАЧНОСТЬ. Заглушка читает `uwTick` ДО вызова штатного обработчика. Если значение
в кадрах растёт, значит `HAL_IncTick` действительно выполняется, то есть прошивка работает как обычно.

ПРИМЕНЕНИЕ (машина сборки, стенд `stand60-home`):
    python scripts/hook/run_vector_hook.py --vector 15 --seconds 6

ГРАНИЦЫ ПРИМЕНИМОСТИ:
  * перехватываются только векторы (обработчики исключений). Произвольную функцию так не перехватить;
  * таблица копируется целиком, иначе пострадают остальные прерывания;
  * заглушка живёт в SRAM: нужен свободный исполняемый регион (у нас `0x2001E000..0x2001F600`);
  * частота = частота события. SysTick 1 кГц даёт 4000 Б/с — буфер 1024 байта переполняется за 0.25 с,
    поэтому кольцо перезаписывается; для 1 кГц смотрите только последние кадры;
  * `VTOR` сбрасывается на reset, поэтому после опыта всё возвращается само.
"""
import argparse
import pathlib
import socket
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[4]
TOOLS = pathlib.Path(__file__).resolve().parents[1]
STAND = "stand60-home"

RTT_CB = 0x2001E000
RTT_BUFFER = 0x2001E100
RTT_SIZE = 1024
RTT_WROFF = 36
VECTORS = 0x2001F000
STUB = 0x2001F400
RTT_PORT = 19021
VTOR_ADDRESS = 0xE000ED08
DEFAULT_ELF = "build/firmware.elf"
# Исключения Cortex-M: SysTick = 15.
KNOWN_VECTORS = {"SysTick": 15, "PendSV": 14, "SVC": 11, "HardFault": 3}


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
    raise ValueError("символ не найден в %s: %s" % (elf, symbol))


def vector_table(elf):
    """Адреса обработчиков из .isr_vector (с битом Thumb, как их видит ядро)."""
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


def build_stub(elf, original_handler, tick_var):
    result = subprocess.run(
        ["arm-none-eabi-as", "-mcpu=cortex-m4", "-mthumb",
         "--defsym", "RTT_CB=0x%X" % RTT_CB, "--defsym", "RTT_WROFF=%d" % RTT_WROFF,
         "--defsym", "RTT_BUF=0x%X" % RTT_BUFFER, "--defsym", "RTT_SIZE=%d" % RTT_SIZE,
         "--defsym", "TICK_VAR=0x%X" % tick_var,
         "--defsym", "ORIG_HANDLER=0x%X" % original_handler,
         "-o", str(ROOT / "build" / "vector_hook.o"), str(TOOLS / "hook" / "vector_hook.S")],
        capture_output=True, text=True, cwd=ROOT)
    if result.returncode:
        raise ValueError("ассемблирование: %s" % result.stderr[:300])
    out = ROOT / "build" / "vector_hook.bin"
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary",
                    str(ROOT / "build" / "vector_hook.o"), str(out)], check=True, cwd=ROOT)
    return out.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vector", default="15",
                        help="номер исключения или имя (SysTick, PendSV, SVC, HardFault)")
    parser.add_argument("--elf", default=DEFAULT_ELF,
                        help="ELF сборки, которая прошита на плате (по умолчанию текущий пресет)")
    parser.add_argument("--seconds", type=float, default=6.0, help="сколько секунд наблюдать")
    parser.add_argument("--keep-openocd", action="store_true")
    args = parser.parse_args()

    vector = int(KNOWN_VECTORS.get(args.vector, args.vector))
    if not 0 <= vector < 16:
        print("ОШИБКА: номер исключения должен быть 0..15")
        return 1
    elf = pathlib.Path(args.elf)
    if not elf.is_file():
        print("ОШИБКА: нет ELF %s" % elf)
        return 1

    print("=== 1. Что перехватываем ===")
    table = vector_table(elf)
    if vector >= len(table):
        print("ОШИБКА: в таблице только %d записей" % len(table))
        return 1
    original = table[vector]
    tick_var = nm("uwTick", elf)
    print("  сборка:   %s" % elf)
    print("  исключение: %d, штатный обработчик: 0x%08X" % (vector, original))
    print("  uwTick:   0x%08X" % tick_var)

    print("\n=== 2. Заглушка и образ ===")
    stub = build_stub(elf, original, tick_var)
    print("  заглушка: %d байт" % len(stub))

    size = STUB + 0x200 - RTT_CB
    image = bytearray(size)
    block = bytearray(48)
    block[0:16] = b"SEGGER RTT" + b"\0" * 6
    struct.pack_into("<i", block, 16, 1)            # MaxNumUpBuffers
    struct.pack_into("<i", block, 20, 0)            # MaxNumDownBuffers
    struct.pack_into("<I", block, 24 + 4, RTT_BUFFER)
    struct.pack_into("<I", block, 24 + 8, RTT_SIZE)
    image[0:48] = block

    vectors = list(table)
    vectors[vector] = STUB | 1                      # бит Thumb
    table_bytes = b"".join(struct.pack("<I", word) for word in vectors)
    image[VECTORS - RTT_CB:VECTORS - RTT_CB + len(table_bytes)] = table_bytes
    image[STUB - RTT_CB:STUB - RTT_CB + len(stub)] = stub

    image_path = ROOT / "build" / "vector-hook-image.bin"
    image_path.write_bytes(bytes(image))
    print("  образ: %s (%d байт)" % (image_path.name, len(image)))
    print("  раскладка: блок 0x%08X, буфер 0x%08X, таблица 0x%08X (%d записей), заглушка 0x%08X"
          % (RTT_CB, RTT_BUFFER, VECTORS, len(vectors), STUB))

    print("\n=== 3. OpenOCD ===")
    subprocess.run(["scp", "-q", str(TOOLS / "start_openocd.sh"), f"{STAND}:/tmp/start_openocd.sh"],
                   capture_output=True)
    result = ssh("sh /tmp/start_openocd.sh 2>&1 | tail -2")
    print("  " + result.stdout.strip().replace("\n", "\n  "))

    tunnels = subprocess.Popen(
        ["ssh", "-N", "-L", "3333:127.0.0.1:3333", "-L", "4444:127.0.0.1:4444",
         "-L", f"{RTT_PORT}:127.0.0.1:{RTT_PORT}", STAND],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    gdb = ["arm-none-eabi-gdb", "-q", "-batch", str(elf)]

    script = ROOT / "build" / "vector_hook.gdb"
    script.write_text("\n".join([
        "echo === VTOR до перехвата ===",
        "printf \"VTOR: 0x%%08x\\n\", *(unsigned int*)0x%08X" % VTOR_ADDRESS,
        "echo === заливка образа в SRAM ===",
        "restore %s binary 0x%08X" % (image_path.as_posix(), RTT_CB),
        "echo === переключение VTOR ===",
        "set *(unsigned int*)0x%08X = 0x%08X" % (VTOR_ADDRESS, VECTORS),
        "printf \"VTOR после: 0x%%08x\\n\", *(unsigned int*)0x%08X" % VTOR_ADDRESS,
        "printf \"вектор %d в копии: 0x%%08x\\n\", *(unsigned int*)(0x%08X + %d)"
        % (vector, VECTORS, vector * 4),
        "echo === проверка заглушки ===",
        "x/3i 0x%08X" % STUB,
        "",
    ]), encoding="utf-8")
    (ROOT / "build" / "vector_hook.gdb").write_text(script.read_text(encoding="utf-8"),
                                                    encoding="utf-8")

    try:
        print("\n=== 4. Установка перехвата ===")
        result = subprocess.run(gdb + ["-ex", "target remote 127.0.0.1:3333", "-x", str(script)],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", cwd=ROOT)
        for line in (result.stdout + result.stderr).splitlines():
            if any(k in line for k in ("VTOR", "вектор", "0x2001", "заливка", "переключение",
                                       "Error", "error")):
                print("  " + line.strip())

        for command in ('rtt setup 0x%X 0x1000 "SEGGER RTT"' % RTT_CB, "rtt start",
                        "rtt server start %d 0" % RTT_PORT):
            answer = telnet(command)
            if "found" in answer:
                print("  Control block found")

        print("\n=== 5. Наблюдение %.0f с ===" % args.seconds)
        process = subprocess.Popen(gdb + ["-ex", "target remote 127.0.0.1:3333",
                                          "-ex", "continue"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
        time.sleep(1.5)
        running = process.poll() is None
        data = b""
        try:
            with socket.create_connection(("127.0.0.1", RTT_PORT), timeout=10) as sock:
                sock.settimeout(2.0)
                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    data += chunk
        except OSError as error:
            print("  ошибка чтения: %s" % error)
        process.kill()
        process.wait(timeout=10)
        print("  цель работает: %s" % ("да" if running else "не подтверждено"))

        print("\n=== 6. Кадры наблюдения ===")
        frames = [data[i:i + 4] for i in range(0, len(data) - 3, 4)]
        samples = [f for f in frames if f and f[0] == 0x53]
        print("  байт: %d, кадров: %d" % (len(data), len(samples)))
        if samples:
            ticks = [f[1] | (f[2] << 8) | (f[3] << 16) for f in samples]
            print("  тики: %s%s" % (ticks[:6], " ..." if len(ticks) > 6 else ""))
            print("  последние: %s" % ticks[-4:])
            increasing = all(b >= a for a, b in zip(ticks, ticks[1:]))
            print("  тики не убывают: %s" % ("да" if increasing else "нет"))
            if len(ticks) > 1 and ticks[-1] > ticks[0]:
                print("  ПРОЗРАЧНОСТЬ ПОДТВЕРЖДЕНА: uwTick растёт => штатный HAL_IncTick работает")
            elif ticks[-1] == ticks[0]:
                print("  ВНИМАНИЕ: uwTick не растёт — штатный обработчик не вызывается")
            # Скорость: по разнице тиков и времени наблюдения.
            if len(ticks) > 1:
                print("  кадров за окно: %d" % len(ticks))
        else:
            print("  кадров нет")

        print("\n=== 7. Снятие перехвата ===")
        restore = ROOT / "build" / "vector_hook_restore.gdb"
        restore.write_text("\n".join([
            "set *(unsigned int*)0x%08X = 0" % VTOR_ADDRESS,
            "printf \"VTOR возвращён: 0x%%08x\\n\", *(unsigned int*)0x%08X" % VTOR_ADDRESS,
            "",
        ]), encoding="utf-8")
        result = subprocess.run(gdb + ["-ex", "target remote 127.0.0.1:3333", "-x", str(restore)],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", cwd=ROOT)
        for line in (result.stdout + result.stderr).splitlines():
            if "VTOR" in line:
                print("  " + line.strip())
    finally:
        tunnels.terminate()
        if not args.keep_openocd:
            ssh("pkill -9 -f '/usr/bin/openocd' 2>/dev/null; sleep 1; "
                "ps -eo comm | grep -qw openocd && echo '  openocd остался' "
                "|| echo '  openocd остановлен, пробник свободен'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
