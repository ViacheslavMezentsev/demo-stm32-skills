"""Перехват возврата функции: получаем её РЕЗУЛЬТАТ, не меняя прошивку.

ЧТО ДЕЛАЕТ. Ставит аппаратную точку останова на входе функции. При попадании запоминает текущий `lr`
(адрес, куда функция вернётся) в ячейку в SRAM и подменяет `lr` на заглушку. Функция отрабатывает как
обычно; когда она возвращается, управление получает заглушка, а в `r0` уже лежит **возвращаемое
значение**. Заглушка пишет его в RTT и уходит по исходному адресу возврата — вызывающий код ничего не
замечает.

ЗАЧЕМ ЭТОТ ВАРИАНТ. Он даёт то, чего не дают остальные:
  * перехват вектора (VTOR) — только момент прерывания;
  * точка останова на входе — аргументы, но не результат;
  * **этот вариант — именно возвращаемое значение** (для float это его биты).

ГРАНИЦЫ ПРИМЕНИМОСТИ:
  * подмену делает отладчик, значит одна остановка на вызов: та же цена, что у точки останова —
    порядка секунды на вызов, только для редких функций;
  * работает для функций, которые возвращаются обычным образом; если функция не возвращается
    (бесконечный цикл, `longjmp`, сброс) — перехвата не будет;
  * подмена `lr` — это изменение хода исполнения; ошибка в заглушке роняет цель (лечится перезагрузкой,
    флеш не затрагивается);
  * на ядре ARMv7-M `lr` после `bl` содержит бит Thumb (1), поэтому адрес заглушки тоже ставится с ним.

ЗАПУСК:
    python scripts/hook/run_retval_hook.py \
        --symbol _ZN13SensorManager11getCpuUsageEv --seconds 10
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
DEFAULT_ELF = "build/firmware.elf"

RTT_CB = 0x2001E000
RTT_BUFFER = 0x2001E100
RTT_SIZE = 1024
RTT_WROFF = 36
STUB = 0x2001F400
RETURN_TO = 0x2001F600
RTT_PORT = 19021


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


def build_stub(tick_var):
    result = subprocess.run(
        ["arm-none-eabi-as", "-mcpu=cortex-m4", "-mthumb",
         "--defsym", "RTT_CB=0x%X" % RTT_CB, "--defsym", "RTT_WROFF=%d" % RTT_WROFF,
         "--defsym", "RTT_BUF=0x%X" % RTT_BUFFER, "--defsym", "RTT_SIZE=%d" % RTT_SIZE,
         "--defsym", "TICK_VAR=0x%X" % tick_var,
         "--defsym", "RETURN_TO=0x%X" % RETURN_TO,
         "-o", str(ROOT / "build" / "retval_hook.o"), str(TOOLS / "hook" / "retval_hook.S")],
        capture_output=True, text=True, cwd=ROOT)
    if result.returncode:
        raise ValueError("ассемблирование: %s" % result.stderr[:300])
    out = ROOT / "build" / "retval_hook.bin"
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary",
                    str(ROOT / "build" / "retval_hook.o"), str(out)], check=True, cwd=ROOT)
    return out.read_bytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="_ZN13SensorManager11getCpuUsageEv",
                        help="функция, чей результат нужен")
    parser.add_argument("--elf", default=DEFAULT_ELF)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--as-float", action="store_true", default=True,
                        help="интерпретировать результат как float (по умолчанию да)")
    args = parser.parse_args()

    entry = nm(args.symbol, args.elf)
    tick = nm("uwTick", args.elf)
    print("=== 1. Что перехватываем ===")
    print("  функция: 0x%08X (%s)" % (entry, args.symbol))
    print("  механизм: подмена lr при входе; результат читается в r0 на возврате")
    print("  заглушка: 0x%08X, ячейка возврата: 0x%08X" % (STUB, RETURN_TO))

    print("\n=== 2. Заглушка и образ ===")
    stub = build_stub(tick)
    print("  заглушка: %d байт" % len(stub))
    size = RETURN_TO + 4 - RTT_CB
    image = bytearray(size)
    block = bytearray(48)
    block[0:16] = b"SEGGER RTT" + b"\0" * 6
    struct.pack_into("<i", block, 16, 1)
    struct.pack_into("<i", block, 20, 0)
    struct.pack_into("<I", block, 24 + 4, RTT_BUFFER)
    struct.pack_into("<I", block, 24 + 8, RTT_SIZE)
    image[0:48] = block
    image[STUB - RTT_CB:STUB - RTT_CB + len(stub)] = stub
    image_path = ROOT / "build" / "retval-image.bin"
    image_path.write_bytes(bytes(image))
    print("  образ: %s (%d байт)" % (image_path.name, len(image)))

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
    gdb = ["arm-none-eabi-gdb", "-q", "-batch", args.elf]

    script = ROOT / "build" / "retval_hook.gdb"
    script.write_text("\n".join([
        "set pagination off",
        "target remote 127.0.0.1:3333",
        "echo === заливка образа ===",
        "restore %s binary 0x%08X" % (image_path.as_posix(), RTT_CB),
        "hbreak *0x%08X" % entry,
        "echo === точка останова и подмена lr ===",
        "info breakpoints",
        "commands",
        "silent",
        # Запоминаем настоящий адрес возврата и отправляем возврат в заглушку.
        "set {int}0x%08X = $lr" % RETURN_TO,
        "set $lr = 0x%08X" % (STUB | 1),
        "continue",
        "end",
        "continue",
        "",
    ]), encoding="utf-8")

    try:
        print("\n=== 4. Перехват ===")
        process = subprocess.Popen(gdb + ["-x", str(script)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", cwd=ROOT)
        time.sleep(1.0)
        for command in ('rtt setup 0x%X 0x1000 "SEGGER RTT"' % RTT_CB, "rtt start",
                        "rtt server start %d 0" % RTT_PORT):
            answer = telnet(command)
            if "found" in answer:
                print("  Control block found")

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
        running = process.poll() is None
        process.kill()
        output = ""
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        print("  цель работала всё время: %s" % ("да" if running else "нет"))
        for line in (output or "").splitlines():
            if "Hardware assisted" in line or "hbreak" in line:
                print("  " + line.strip()[:110])

        print("\n=== 5. Результаты функции ===")
        frames = [data[i:i + 8] for i in range(0, len(data) - 7, 8)]
        rets = [f for f in frames if f and f[0] == 0x52]
        print("  байт: %d, кадров-возвратов: %d" % (len(data), len(rets)))
        if rets:
            values = [struct.unpack("<I", bytes(f[1:5]))[0] for f in rets]
            ticks = [f[5] | (f[6] << 8) | (f[7] << 16) for f in rets]
            print("  как float:")
            for value, stamp in list(zip(values, ticks))[:8]:
                print("    тик %-8d -> %.3f (0x%08X)" % (stamp, struct.unpack("<f", struct.pack("<I", value))[0], value))
            print("  как целые: %s" % values[:8])
            print("  ПЕРЕХВАТ ВОЗВРАТА РАБОТАЕТ: получено значений %d" % len(values))
        else:
            print("  возвратов не зафиксировано (функция не вызывалась?)")
    finally:
        tunnels.terminate()
        ssh("pkill -9 -f '/usr/bin/openocd' 2>/dev/null; sleep 1; "
            "ps -eo comm | grep -qw openocd && echo '  openocd остался' "
            "|| echo '  openocd остановлен, пробник свободен'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
