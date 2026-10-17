#!/usr/bin/env python3
"""SWO/ITM для STM32 через OpenOCD: проверка, приём потока, счётчики DWT.

    python swo_trace.py --check                  # доступен ли SWO и настроен ли TPIU
    python swo_trace.py --capture --seconds 5    # принять поток ITM
    python swo_trace.py --dwt --seconds 5        # снять DWT CYCCNT до/после
"""
import argparse
import re
import socket
import subprocess
import sys
import time


def openocd_command(args):
    return [args.openocd, "-f", "interface/stlink.cfg", "-f", "target/stm32f4x.cfg",
            "-c", "gdb_port 3333", "-c", "telnet_port 4444",
            "-c", "tpiu config internal - uart off %d" % args.sysclk,
            "-c", "itm ports on", "-c", "init", "-c", "reset run"]


def telnet(commands, port=4444, timeout=10.0):
    answer = []
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        for command in commands:
            sock.sendall((command + "\n").encode())
            time.sleep(0.3)
            try:
                answer.append(sock.recv(65536).decode("utf-8", "replace"))
            except socket.timeout:
                answer.append("")
    return answer


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--dwt", action="store_true")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--sysclk", type=int, default=72000000, help="частота ядра, Гц")
    parser.add_argument("--openocd", default="openocd")
    args = parser.parse_args()

    if not (args.check or args.capture or args.dwt):
        parser.print_help()
        return 2

    process = subprocess.Popen(openocd_command(args), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        for _attempt in range(1, 16):
            time.sleep(1.0)
            try:
                socket.create_connection(("127.0.0.1", 4444), timeout=1).close()
                break
            except OSError:
                continue
        else:
            print("ОШИБКА: OpenOCD не поднялся на порт 4444")
            return 1

        if args.check:
            reply = "".join(telnet(["tpiu config"]))
            print("TPIU:", reply.strip()[:300] or "команда не ответила")
            print("Проверьте, что вывод SWO не занят другой функцией MCU (у F411 это PB3).")

        if args.capture:
            print("приём потока %.0f с..." % args.seconds)
            text = "".join(telnet(["itm port 0 on"], timeout=args.seconds + 5))
            lines = [line for line in text.splitlines() if line.strip()]
            print("\n".join(lines)[:2000] if lines else "(поток пуст — включён ли ITM в прошивке?)")

        if args.dwt:
            before = "".join(telnet(["mdw 0xE0001004 1"]))
            time.sleep(args.seconds)
            after = "".join(telnet(["mdw 0xE0001004 1"]))
            print("DWT CYCCNT до:    ", re.sub(r"\s+", " ", before)[-60:])
            print("DWT CYCCNT после: ", re.sub(r"\s+", " ", after)[-60:])
    finally:
        process.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
