"""PoC, часть 3: переживает ли блок RTT запуск прошивки и можно ли читать его на ходу.

Практическая ценность метода зависит от этого: если блок живёт только на остановленном ядре, толку мало.
Здесь проверяется:
  1. блок залит, цель отпущена (`continue`) — прошивка работает, писала бы в него при наличии кода;
  2. через паузу OpenOCD снова читает блок по тому же адресу — структура не затёрта;
  3. читается поток с RTT-сервера, пока прошивка исполняется.

Ожидаемые ограничения, которые надо зафиксировать: прошивка ничего не знает о блоке и в буфер не
пишет, поэтому новых данных без внешнего писателя не появится; однако сам канал доступен.

Запуск:
    python scripts/run_running.py
"""
import pathlib
import socket
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path.cwd()  # корень проекта прошивки: запускать из него (build/firmware.elf)
TOOLS = pathlib.Path(__file__).resolve().parent
STAND = "stand60-home"
BLOCK_ADDRESS = 0x2001E000
BUFFER_ADDRESS = 0x2001E100
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


def main():
    print("=== 1. OpenOCD, инъекция, RTT ===")
    subprocess.run(["scp", "-q", str(TOOLS / "start_openocd.sh"), f"{STAND}:/tmp/start_openocd.sh"],
                   capture_output=True)
    result = ssh("sh /tmp/start_openocd.sh 2>&1 | tail -3")
    print("  " + result.stdout.strip().replace("\n", "\n  "))

    image = ROOT / "build" / "rtt-block.bin"
    subprocess.run([sys.executable, str(TOOLS / "build_block.py"), "--out", str(image),
                    "--address", hex(BLOCK_ADDRESS), "--buffer", hex(BUFFER_ADDRESS)],
                   check=True, stdout=subprocess.DEVNULL)
    inject = ROOT / "build" / "inject_block.gdb"
    template = (TOOLS / "inject_block.gdb.in").read_text(encoding="utf-8")
    inject.write_text(template.replace("@IMAGE_PATH@", image.as_posix()), encoding="utf-8")

    tunnels = subprocess.Popen(
        ["ssh", "-N", "-L", "3333:127.0.0.1:3333", "-L", "4444:127.0.0.1:4444",
         "-L", f"{RTT_PORT}:127.0.0.1:{RTT_PORT}", STAND],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    gdb = ["arm-none-eabi-gdb", "-q", "-batch", "build/firmware.elf"]
    try:
        subprocess.run(gdb + ["-ex", "target remote 127.0.0.1:3333", "-x", str(inject)],
                       capture_output=True, cwd=ROOT)

        # Настройка RTT и сервер.
        for command in ('rtt setup 0x%X 0x1000 "SEGGER RTT"' % BLOCK_ADDRESS, "rtt start",
                        "rtt server start %d 0" % RTT_PORT):
            answer = telnet(command)
            if "found" in answer:
                print("  " + " ".join(answer.split())[-70:])

        print("\n=== 2. Отпускаем цель (прошивка работает) ===")
        # `continue` в batch-режиме не возвращает управление, пока цель не остановится: это и есть
        # признак того, что прошивка пошла. Поэтому тайм-аут — ожидаемый исход, а не ошибка.
        running = False
        try:
            process = subprocess.Popen(gdb + ["-ex", "target remote 127.0.0.1:3333",
                                              "-ex", "continue"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       cwd=ROOT)
            time.sleep(5)
            running = process.poll() is None
            process.kill()
            process.wait(timeout=10)
        except OSError as error:
            print("  ошибка запуска GDB: %s" % error)
        print("  цель отпущена и работает: %s" % ("да" if running else "не подтверждено"))

        print("\n=== 3. Чтение потока при работающей прошивке ===")
        received = b""
        try:
            with socket.create_connection(("127.0.0.1", RTT_PORT), timeout=10) as sock:
                sock.settimeout(2.0)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    received += chunk
        except OSError as error:
            print("  ошибка чтения: %s" % error)
        print("  получено байт: %d" % len(received))
        print("  данные: %r" % received[:120])

        print("\n=== 4. Цел ли блок после работы прошивки ===")
        check = ("python3 - <<'EOF'\n"
                 "import socket, time\n"
                 "try:\n"
                 "    s = socket.create_connection(('127.0.0.1', 4444), timeout=5)\n"
                 "    s.sendall(b'rtt channels\\n')\n"
                 "    time.sleep(1)\n"
                 "    print(s.recv(4096).decode('utf-8', 'replace'))\n"
                 "except Exception as e:\n"
                 "    print('ERROR', e)\n"
                 "EOF\n")
        print("  (проверка каналов через telnet выполнена ниже)")
        answer = telnet("rtt channels")
        print("  " + " ".join(answer.split())[:200])
    finally:
        tunnels.terminate()
        ssh("pkill -f 'openocd.*stlink' 2>/dev/null; echo stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
