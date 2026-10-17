"""PoC с перехватом управления: RTT наполняет заглушка в SRAM, без изменения прошивки.

Отличие от первого PoC: там канал RTT наполнял отладчик, а здесь в цепочку обработки вставляется
**посредник**. Механизм штатный для Cortex-M: регистр `SCB->VTOR` задаёт, откуда ядро берёт таблицу
векторов. Копируем таблицу в SRAM, в копии подменяем `SysTick` на заглушку, заглушка пишет в RTT и
вызывает прежний обработчик. **Прошивка в флеше не меняется ни на байт** — меняется только источник
таблицы векторов.

Аналогия с ПК, о которой шла речь: это инъекция посредника в цепочку вызовов, только вместо подмены
импорта DLL используется подмена вектора прерывания.

Порядок: OpenOCD → образ (блок RTT + таблица векторов + заглушка) → `VTOR` → `rtt setup/start/server`
→ отпустить цель → читать поток. Заглушка выдаёт 4 байта на тик (1 кГц): 'T' и номер тика.

Запуск:
    python scripts/hook/run_hook.py
"""
import pathlib
import socket
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path.cwd()  # корень проекта прошивки: запускать из него (build/firmware.elf)
TOOLS = pathlib.Path(__file__).resolve().parents[1]
STAND = "stand60-home"
RTT_CB = 0x2001E000
VECTORS = 0x2001F000
RTT_PORT = 19021
# SCB->VTOR: адрес таблицы векторов.
VTOR = 0xE000ED08


def ssh(command, timeout=30):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
                           STAND, command], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout + 20)


def telnet(command, port=4444, timeout=4):
    """Команда в telnet-порт OpenOCD."""
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


def elf_symbol(name, elf="build/firmware.elf"):
    """Адрес символа из ELF. Нужны `uwTick` (счётчик тиков) и `SysTick_Handler` (прежний обработчик)."""
    output = subprocess.run(["arm-none-eabi-nm", elf], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=ROOT)
    for line in output.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == name:
            return int(parts[0], 16)
    raise ValueError("символ не найден в ELF: %s" % name)


def main():
    print("=== 1. OpenOCD на стенде ===")
    subprocess.run(["scp", "-q", str(TOOLS / "start_openocd.sh"), f"{STAND}:/tmp/start_openocd.sh"],
                   capture_output=True)
    result = ssh("sh /tmp/start_openocd.sh 2>&1 | tail -3")
    print("  " + result.stdout.strip().replace("\n", "\n  "))

    print("\n=== 2. Сборка заглушки и образа ===")
    stub = ROOT / "build" / "hook.bin"
    # Адреса берутся из ELF, а не задаются на глаз: `uwTick` — счётчик тиков прошивки, `SysTick_Handler` —
    # прежний обработчик. Если задать их нулями, заглушка читает тик из адреса 0 и уходит в 0 — цель
    # падает, а в канал попадает одно значение (проверено).
    tick_var = elf_symbol("uwTick")
    original_systick = elf_symbol("SysTick_Handler")
    print("  uwTick = 0x%08X (прежний SysTick_Handler = 0x%08X, но заглушка выходит по EXC_RETURN)"
          % (tick_var, original_systick))
    assemble = subprocess.run(
        ["arm-none-eabi-as", "-mcpu=cortex-m4", "-mthumb",
         "--defsym", "RTT_CB=0x%X" % RTT_CB, "--defsym", "RTT_WROFF=36",
         "--defsym", "RTT_BUF=0x2001E100", "--defsym", "RTT_SIZE=1024",
         "--defsym", "TICK_VAR=0x%X" % tick_var,
         "-o", str(ROOT / "build" / "hook.o"), str(TOOLS / "hook" / "hook.S")],
        capture_output=True, text=True)
    if assemble.returncode:
        print("  ошибка ассемблирования: %s" % assemble.stderr[:300])
        return 1
    subprocess.run(["arm-none-eabi-objcopy", "-O", "binary",
                    str(ROOT / "build" / "hook.o"), str(stub)], check=True)
    image = ROOT / "build" / "hook-image.bin"
    result = subprocess.run([sys.executable, str(TOOLS / "hook" / "build_hook_image.py"),
                             "--elf", "build/firmware.elf",
                             "--stub", str(stub), "--out", str(image)],
                            capture_output=True, text=True, cwd=ROOT)
    print("  " + result.stdout.strip().replace("\n", "\n  ")[:400])
    if result.returncode:
        print("  ошибка: %s" % result.stderr[-300:])
        return 1

    # GDB-скрипт: залить образ и перенаправить VTOR. Собирается списком строк, без %-форматирования:
    # в нём есть символы процента, и смешивать их с подстановкой путей опасно (проверено — ошибка
    # «not enough arguments for format string»).
    script = ROOT / "build" / "hook.gdb"
    script.write_text("\n".join([
        "echo === заливка образа перехвата ===\\n",
        "restore %s binary 0x%08X" % (image.as_posix(), RTT_CB),
        "echo === дизассемблер заглушки ===\\n",
        "x/4i 0x2001F200",
        "echo === таблица векторов в SRAM: SysTick ===\\n",
        "printf \"вектор SysTick в копии: 0x%%08x\\n\", *(unsigned int*)(0x%08X + 60)" % VECTORS,
        "printf \"VTOR до:    0x%%08x\\n\", *(unsigned int*)0x%08X" % VTOR,
        "set *(unsigned int*)0x%08X = 0x%08X" % (VTOR, VECTORS),
        "printf \"VTOR после: 0x%%08x\\n\", *(unsigned int*)0x%08X" % VTOR,
        "echo === готово ===\\n",
        "",
    ]), encoding="utf-8")

    tunnels = subprocess.Popen(
        ["ssh", "-N", "-L", "3333:127.0.0.1:3333", "-L", "4444:127.0.0.1:4444",
         "-L", f"{RTT_PORT}:127.0.0.1:{RTT_PORT}", STAND],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    gdb = ["arm-none-eabi-gdb", "-q", "-batch", "build/firmware.elf"]
    try:
        print("\n=== 3. Заливка и подмена VTOR ===")
        result = subprocess.run(gdb + ["-ex", "target remote 127.0.0.1:3333", "-x", str(script)],
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", cwd=ROOT)
        for line in (result.stdout + result.stderr).splitlines():
            if any(key in line for key in ("VTOR", "вектор", "было бы", "заливка", "$1 =", "0x2001")):
                print("  " + line.strip())

        print("\n=== 4. RTT ===")
        for command in ('rtt setup 0x%X 0x1000 "SEGGER RTT"' % RTT_CB, "rtt start",
                        "rtt server start %d 0" % RTT_PORT):
            answer = telnet(command)
            if "found" in answer:
                print("  Control block found")

        print("\n=== 5. Отпускаем цель — заглушка должна писать сама ===")
        process = subprocess.Popen(gdb + ["-ex", "target remote 127.0.0.1:3333",
                                          "-ex", "continue"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
        time.sleep(6)
        running = process.poll() is None
        print("  цель работает: %s" % ("да" if running else "не подтверждено"))

        data = b""
        try:
            with socket.create_connection(("127.0.0.1", RTT_PORT), timeout=10) as sock:
                sock.settimeout(2.0)
                deadline = time.monotonic() + 8
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

        print("\n=== 6. Что пришло ===")
        print("  получено байт: %d" % len(data))
        if data:
            ticks = [struct.unpack_from("<I", data, offset)[0] >> 8
                     for offset in range(0, len(data) - 3, 4)]
            markers = sum(1 for offset in range(0, len(data) - 3, 4) if data[offset] == 0x54)
            print("  кадров с маркером 'T': %d" % markers)
            if ticks:
                print("  номера тиков: %s%s" % (ticks[:8], " ..." if len(ticks) > 8 else ""))
                increasing = all(b >= a for a, b in zip(ticks, ticks[1:]))
                print("  тики возрастают: %s" % ("да" if increasing else "нет"))
                if markers and increasing:
                    print("  ПЕРЕХВАТ РАБОТАЕТ: данные пишет заглушка, прошивка не менялась")
                else:
                    print("  перехват сработал, но данные подозрительны")
            print("  первые байты: %r" % data[:32])
        else:
            print("  данных нет")
    finally:
        tunnels.terminate()
        ssh("pkill -9 -f '/usr/bin/openocd' 2>/dev/null; sleep 1; "
            "ps -eo comm | grep -qw openocd && echo '  openocd остался' || echo '  openocd остановлен'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
