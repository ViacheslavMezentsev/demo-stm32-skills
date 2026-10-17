"""PoC: управляющий блок RTT в SRAM платы — без изменения прошивки.

Проверяется гипотеза: **RTT можно использовать без поддержки в прошивке.** Для этого достаточно, чтобы
в памяти существовал управляющий блок SEGGER RTT с валидными указателями — прошить код, который в него
пишет, не обязательно. Блок создаётся инъекцией отладчика в свободную область RAM (выше `.bss` и ниже
стека), а данные в кольцевом буфере появляются так же, как если бы их положила прошивка.

Почему это вообще работает: OpenOCD ищет блок по строке `SEGGER RTT` в заданном окне памяти и читает
кольцевой буфер по указателям из блока. Ему безразлично, кто заполнил структуру — код прошивки или
отладчик, и кто пишет в буфер.

Порядок проверки:
  1. поднять OpenOCD на стенде (GDB-порт и telnet);
  2. собрать образ блока на хосте (`build_block.py`) — поля упакованы точно;
  3. залить образ в SRAM через GDB (`restore binary`);
  4. через telnet: `rtt setup`, `rtt start`, `rtt channels`, `rtt server start`;
  5. прочитать поток с RTT-сервера и сверить с отправленным.

Что доказывает успех: OpenOCD находит блок, видит канал нужного размера, сервер отдаёт **ровно те
байты**, которые положены в буфер, и `WrOff` после чтения сдвигается хостом.

Запуск на машине сборки:
    python scripts/run_poc.py
"""
import argparse
import pathlib
import re
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path.cwd()  # корень проекта прошивки: запускать из него (build/firmware.elf)
TOOLS = pathlib.Path(__file__).resolve().parent
STAND = "stand60-home"
BLOCK_ADDRESS = 0x2001E000
BUFFER_ADDRESS = 0x2001E100
RTT_PORT = 19021
PAYLOAD = b"RTT injected into SRAM: no firmware change\n"


def ssh(command, timeout=30):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
                           STAND, command], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout + 20)


def start_openocd():
    """Поднять OpenOCD на стенде (без RTT: блок появится позже)."""
    subprocess.run(["scp", "-q", str(TOOLS / "start_openocd.sh"), f"{STAND}:/tmp/start_openocd.sh"],
                   capture_output=True)
    result = ssh("sh /tmp/start_openocd.sh 2>&1 | tail -8")
    print(result.stdout.strip())
    return "Listening on port 3333" in result.stdout


def inject():
    """Сгенерировать GDB-скрипт с путём к образу и залить блок через SSH-туннель."""
    image = ROOT / "build" / "rtt-block.bin"
    subprocess.run([sys.executable, str(TOOLS / "build_block.py"), "--out", str(image),
                    "--address", hex(BLOCK_ADDRESS), "--buffer", hex(BUFFER_ADDRESS)],
                   check=True)
    script = ROOT / "build" / "inject_block.gdb"
    template = (TOOLS / "inject_block.gdb.in").read_text(encoding="utf-8")
    script.write_text(template.replace("@IMAGE_PATH@", image.as_posix()), encoding="utf-8")

    tunnel = subprocess.Popen(["ssh", "-N", "-L", "3333:127.0.0.1:3333", STAND],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        gdb = "arm-none-eabi-gdb"
        result = subprocess.run([gdb, "-q", "-batch", "build/firmware.elf",
                                 "-ex", "set pagination off",
                                 "-ex", "target remote 127.0.0.1:3333",
                                 "-x", str(script)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", cwd=ROOT)
        return result.stdout + result.stderr
    finally:
        tunnel.terminate()


def telnet(command, port=4444, timeout=4):
    """Послать команду в telnet-порт OpenOCD и вернуть ответ."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall((command + "\n").encode())
            time.sleep(0.4)
            try:
                # Порт открыт на стенде, поэтому туннель нужен и здесь.
                chunks = []
                sock.settimeout(1.0)
                while True:
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not data:
                        break
                    chunks.append(data)
                return b"".join(chunks).decode("utf-8", "replace")
            except OSError:
                return ""
    except OSError as error:
        return "ОШИБКА подключения к telnet: %s" % error


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-openocd", action="store_true", help="не останавливать OpenOCD")
    args = parser.parse_args()

    print("=== 1. OpenOCD на стенде ===")
    if not start_openocd():
        print("ОШИБКА: OpenOCD не поднялся")
        return 1

    print("\n=== 2. Инъекция блока в SRAM ===")
    output = inject()
    for line in output.splitlines():
        if any(key in line for key in ("загрузка", "ID блока", "MaxNum", "pBuffer", "SizeOf",
                                       "WrOff", "RdOff", "первые", "Restoring")):
            print("  " + line.strip())

    print("\n=== 3. RTT через telnet ===")
    tunnel = subprocess.Popen(["ssh", "-N", "-L", "4444:127.0.0.1:4444",
                               "-L", f"{RTT_PORT}:127.0.0.1:{RTT_PORT}", STAND],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    try:
        for command in ('rtt setup 0x%X 0x1000 "SEGGER RTT"' % BLOCK_ADDRESS, "rtt start",
                        "rtt channels"):
            answer = telnet(command)
            print("  %-42s -> %s" % (command, " ".join(answer.split())[:160]))
        print("\n=== 4. RTT-сервер и чтение потока ===")
        telnet("rtt server start %d 0" % RTT_PORT)
        time.sleep(0.5)
        data = b""
        deadline = time.monotonic() + 6
        with socket.create_connection(("127.0.0.1", RTT_PORT), timeout=6) as sock:
            sock.settimeout(1.0)
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                data += chunk
        print("  получено байт: %d" % len(data))
        print("  данные: %r" % data)
        if data.strip() == PAYLOAD.strip():
            print("  СОВПАЛО с отправленным")
        elif PAYLOAD.strip() in data:
            print("  отправленное содержится в потоке")
        else:
            print("  НЕ СОВПАЛО")
        # Состояние указателей после чтения: хост сдвигает RdOff.
        time.sleep(0.5)
    finally:
        tunnel.terminate()
        if not args.keep_openocd:
            ssh("pkill -f 'openocd.*stlink' 2>/dev/null; echo stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
