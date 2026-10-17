"""PoC, часть 2: живой поток RTT через инъекции без поддержки в прошивке.

Первый шаг доказал, что OpenOCD находит блок, созданный в SRAM, и отдаёт его содержимое. Но это ещё
статичный снимок. Здесь проверяется поток: пока RTT-сервер читает канал, отладчик **дописывает** в
кольцевой буфер новые сообщения и двигает `WrOff` — то есть работает как «внешний поставщик данных».

Что это доказывает: RTT-канал можно наполнять вообще без кода в прошивке. Что это НЕ даёт: прошивка
сама в буфер не пишет, поэтому в штатной работе канал пуст, пока его не наполняет отладчик.

Запуск:
    python scripts/run_live.py
"""
import pathlib
import socket
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOLS = pathlib.Path(__file__).resolve().parent
STAND = "stand60-home"
BLOCK_ADDRESS = 0x2001E000
BUFFER_ADDRESS = 0x2001E100
BUFFER_SIZE = 1024
RTT_PORT = 19021
MESSAGES = 12
# Приветствие, которое кладёт инъекция: его длина задаёт начальный WrOff.
PAYLOAD_HINT = b"RTT injected into SRAM: no firmware change\n"


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


def make_writer_script(path, base_offset):
    """GDB-скрипт: писать сообщения в кольцевой буфер, двигая WrOff.

    Позицию чтения (`RdOff`) писатель НЕ трогает: её ведёт хост, и сброс приводил бы к тому, что часть
    уже отданных байтов прочиталась бы снова, а начало новых сообщений потерялось (проверено: из 12
    сообщений доходило 3). Новые данные пишутся от текущего `WrOff`, указатель двигается ПОСЛЕ записи,
    поэтому хост не видит недописанных сообщений.

    `base_offset` — начальный `WrOff` после инъекции образа: с этого места и продолжается запись.
    """
    lines = ["echo === живая запись в RTT-буфер ===\n"]
    written = 0
    for index in range(MESSAGES):
        message = "live-%02d injected-by-gdb\n" % index
        for offset, char in enumerate(message):
            lines.append("set {char}(0x%08X + %d + %d) = %d\n"
                         % (BUFFER_ADDRESS, base_offset + written, offset, ord(char)))
        written += len(message)
        lines.append("set *(unsigned int*)0x%08X = %d\n"
                     % (BLOCK_ADDRESS + 36, base_offset + written))
        lines.append("shell sleep 0.35\n")
    path.write_text("".join(lines), encoding="utf-8")
    return written


def main():
    print("=== 1. OpenOCD и инъекция блока ===")
    subprocess.run(["scp", "-q", str(TOOLS / "start_openocd.sh"), f"{STAND}:/tmp/start_openocd.sh"],
                   capture_output=True)
    result = ssh("sh /tmp/start_openocd.sh 2>&1 | tail -4")
    print("  " + result.stdout.strip().replace("\n", "\n  "))

    image = ROOT / "build" / "rtt-block.bin"
    subprocess.run([sys.executable, str(TOOLS / "build_block.py"), "--out", str(image),
                    "--address", hex(BLOCK_ADDRESS), "--buffer", hex(BUFFER_ADDRESS)],
                   check=True, stdout=subprocess.DEVNULL)
    inject = ROOT / "build" / "inject_block.gdb"
    template = (TOOLS / "inject_block.gdb.in").read_text(encoding="utf-8")
    inject.write_text(template.replace("@IMAGE_PATH@", image.as_posix()), encoding="utf-8")

    writer = ROOT / "build" / "rtt_writer.gdb"
    # Инъекция кладёт в буфер 43 байта приветствия, и WrOff после неё равен 43 — с этого места и
    # продолжается живая запись.
    total = make_writer_script(writer, base_offset=len(PAYLOAD_HINT))
    print("  подготовлено сообщений: %d, всего байт: %d" % (MESSAGES, total))

    # Туннели: GDB-порт и RTT-сервер открыты на стенде.
    tunnels = subprocess.Popen(
        ["ssh", "-N", "-L", "3333:127.0.0.1:3333", "-L", "4444:127.0.0.1:4444",
         "-L", f"{RTT_PORT}:127.0.0.1:{RTT_PORT}", STAND],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    received = {"data": b""}

    def reader():
        """Читать RTT-сервер, пока пишет отладчик.

        Выход только по тайм-ауту бездействия: данные приходят порциями по мере записи, и обрывать
        чтение по числу строк нельзя — часть сообщений ещё не написана (проверено: обрыв на 4 из 12).
        """
        last_data = time.monotonic()
        try:
            with socket.create_connection(("127.0.0.1", RTT_PORT), timeout=30) as sock:
                sock.settimeout(1.0)
                while time.monotonic() - last_data < 8.0:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    received["data"] += chunk
                    last_data = time.monotonic()
        except OSError as error:
            print("  ошибка чтения: %s" % error)

    try:
        # Инъекция блока (соединение GDB закрывается).
        gdb_base = ["arm-none-eabi-gdb", "-q", "-batch", "build/firmware.elf"]
        subprocess.run(gdb_base + ["-ex", "target remote 127.0.0.1:3333", "-x", str(inject)],
                       capture_output=True, cwd=ROOT)

        print("\n=== 2. RTT: настройка и сервер ===")
        for command in ('rtt setup 0x%X 0x1000 "SEGGER RTT"' % BLOCK_ADDRESS, "rtt start",
                        "rtt server start %d 0" % RTT_PORT):
            answer = telnet(command)
            marker = "Control block found" if "found" in answer else ""
            print("  %-42s %s" % (command, marker or " ".join(answer.split())[:70]))

        print("\n=== 3. Живой поток: отладчик пишет, сервер читает ===")
        # Читатель подключается, когда сервер уже слушает: иначе соединение отклоняется.
        time.sleep(1.0)
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        time.sleep(1.0)
        # Второе соединение GDB: пишем сообщения с паузами. Между записями цель останавливается
        # отладчиком, но после каждого `shell sleep` сервер успевает опросить память и отдать байты.
        write_result = subprocess.run(
            gdb_base + ["-ex", "target remote 127.0.0.1:3333", "-x", str(writer)],
            capture_output=True, cwd=ROOT, text=True, encoding="utf-8", errors="replace")
        print("  запись завершена, код %d" % write_result.returncode)
        if write_result.returncode:
            print("  " + (write_result.stdout or write_result.stderr).strip()[-300:])
        thread.join(timeout=40)

        data = received["data"]
        lines = [line for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
        print("  получено байт: %d, строк: %d" % (len(data), len(lines)))
        for line in lines[:6]:
            print("    %s" % line)
        if len(lines) > 6:
            print("    ... всего %d" % len(lines))
        expected = sum(1 for line in lines if line.startswith("live-"))
        print("  строк вида live-NN: %d из %d" % (expected, MESSAGES))
        if expected >= MESSAGES - 1:
            print("  ЖИВОЙ ПОТОК РАБОТАЕТ")
        else:
            print("  поток неполный")
    finally:
        tunnels.terminate()
        ssh("pkill -f 'openocd.*stlink' 2>/dev/null; echo stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
