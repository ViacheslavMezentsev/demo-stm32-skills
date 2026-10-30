#!/usr/bin/env python3
"""Короткие команды цели через OpenOCD: состояние, чтение, стоп, старт, сброс — локально или по SSH.

Навык stm32-gdbpy-scenario. Без GDB: каждый вызов запускает OpenOCD на один сеанс
(init → команды → shutdown) или отправляет команды уже работающему OpenOCD через его
Tcl-порт. Чтение памяти не останавливает ядро.

  python ocd.py --target stm32f4x --serial <серийник> state read 0xE0042000
  python ocd.py --ssh user@stand --target stm32f1x --serial <серийник> halt state
  python ocd.py --ssh user@stand --target stm32f4x --usb-location 7-1.1.3 reset-halt state resume
  python ocd.py --tcl 127.0.0.1:6666 read 0x20000000 4          # работающий OpenOCD
  python ocd.py --interface interface/jlink.cfg --serial <номер J-Link> --set CPUTAPID=0x2ba01477 \
      --target artery/at32f4x state                             # J-Link через OpenOCD, AT32F403A

Действия (выполняются по порядку в одном сеансе):
  state               DHCSR: RUNNING, HALTED, SLEEPING, LOCKUP
  read ADDR [N]       N слов по 32 бита (по умолчанию 1), формат «0xADDR 0xVALUE»
  halt                остановить ядро; после выхода OpenOCD ядро остаётся остановленным
  resume              продолжить; в одноразовом сеансе сначала halt (иначе OpenOCD не знает состояния)
  reset-run           сброс и запуск
  reset-halt          сброс и остановка на векторе сброса
  raw "команда"       любая команда OpenOCD как есть

Код возврата — код OpenOCD (или 1, если сервер Tcl не ответил).
"""
import argparse
import shlex
import socket
import subprocess
import sys

DHCSR = 0xE000EDF0
ACTIONS = {"state": 0, "read": None, "halt": 0, "resume": 0, "reset-run": 0, "reset-halt": 0, "raw": 1}


def parse_actions(tokens):
    actions, index = [], 0
    while index < len(tokens):
        name = tokens[index]
        if name not in ACTIONS:
            raise SystemExit("неизвестное действие: %s" % name)
        index += 1
        if name == "read":
            args = [tokens[index]]
            index += 1
            if index < len(tokens) and tokens[index] not in ACTIONS:
                args.append(tokens[index])
                index += 1
        else:
            args = tokens[index:index + ACTIONS[name]]
            index += ACTIONS[name]
        actions.append((name, args))
    return actions


def commands(actions, oneshot):
    """Команды OpenOCD; маркеры echo отделяют ответы действий в общем выводе."""
    out = []
    for number, (name, args) in enumerate(actions):
        out.append("echo {@@%d}" % number)
        if name == "state":
            out.append("mdw 0x%08X" % DHCSR)
        elif name == "read":
            out.append("mdw %s %s" % (args[0], args[1] if len(args) > 1 else "1"))
        elif name == "halt":
            out.append("halt")
        elif name == "resume":
            out += (["halt"] if oneshot else []) + ["resume"]
        elif name == "reset-run":
            out.append("reset run")
        elif name == "reset-halt":
            out.append("reset halt")
        else:
            out.append(args[0])
    out.append("echo {@@end}")
    return out


def decode_state(value):
    if value & (1 << 19):
        return "LOCKUP"
    if value & (1 << 17):
        return "HALTED"
    if value & (1 << 18):
        return "SLEEPING"
    return "RUNNING"


def words(text):
    rows = []
    for line in text.splitlines():
        head, sep, tail = line.strip().partition(":")
        if sep and head.lower().startswith("0x"):
            address = int(head, 16)
            for offset, item in enumerate(tail.split()):
                rows.append((address + 4 * offset, int(item, 16)))
    return rows


def report(actions, text):
    """Печать по действиям: адрес и значение, состояние, ошибки OpenOCD."""
    parts = text.split("@@")
    chunks = {}
    for part in parts[1:]:
        key, _, body = part.partition("\n")
        chunks[key.strip()] = body
    for number, (name, args) in enumerate(actions):
        body = chunks.get(str(number), "")
        errors = [line.strip() for line in body.splitlines() if line.startswith("Error")]
        if name == "state":
            rows = words(body)
            print("state %s (DHCSR 0x%08X)" % (decode_state(rows[0][1]), rows[0][1]) if rows else "state ?")
        elif name == "read":
            for address, value in words(body):
                print("0x%08X 0x%08X" % (address, value))
        elif name == "raw":
            print(body.strip())
        else:
            print(name + (" ERROR" if errors else " ok"))
        for line in errors:
            print("  " + line)


def remote_path(path):
    """Путь для оболочки узла: ~/, $HOME/ и ${HOME}/ раскрываются в домашний каталог, остальное экранируется."""
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if path.startswith(prefix):
            return '"$HOME"/' + shlex.quote(path[len(prefix):])
    return shlex.quote(path)


def oneshot(args, actions):
    command = [args.openocd, "-f", args.interface]
    if args.transport:
        command += ["-c", "transport select " + args.transport]
    if args.serial:
        command += ["-c", "adapter serial " + args.serial]
    if args.usb_location:
        command += ["-c", "adapter usb location " + args.usb_location]
    for item in args.set:
        name, _, value = item.partition("=")
        command += ["-c", "set %s %s" % (name, value)]
    target = args.target if args.target.endswith(".cfg") else "target/%s.cfg" % args.target
    command += ["-f", target, "-c", "init"]
    for item in commands(actions, True):
        command += ["-c", item]
    command += ["-c", "shutdown"]
    if args.ssh:
        remote = " ".join([remote_path(command[0])] + [shlex.quote(part) for part in command[1:]]) + " 2>&1"
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        command += (["-i", args.ssh_key] if args.ssh_key else []) + [args.ssh, remote]
    # Один поток: эхо-маркеры и ответы команд идут в порядке выполнения.
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          timeout=args.timeout, errors="replace")
    return proc.returncode, proc.stdout


def tcl(args, actions):
    """Работающий OpenOCD: Tcl-порт, команды через capture, разделитель 0x1a."""
    host, _, port = args.tcl.rpartition(":")
    text = []
    with socket.create_connection((host or "127.0.0.1", int(port)), timeout=args.timeout) as sock:
        for item in commands(actions, False):
            sock.sendall(("capture {%s}" % item).encode() + b"\x1a")
            reply = b""
            while not reply.endswith(b"\x1a"):
                chunk = sock.recv(4096)
                if not chunk:
                    return 1, "".join(text) + "\nError: connection closed"
                reply += chunk
            answer = reply[:-1].decode(errors="replace")
            text.append(answer if answer.endswith("\n") or not answer else answer + "\n")
    return 0, "".join(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", help="target/<имя>.cfg OpenOCD или путь к .cfg (для одноразового сеанса)")
    parser.add_argument("--interface", default="interface/stlink.cfg", help="interface/stlink.cfg, interface/jlink.cfg, ...")
    parser.add_argument("--transport", choices=("swd", "jtag"), help="transport select: нужен J-Link и CMSIS-DAP")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                        help="переменная Tcl до конфига цели, например CPUTAPID=0x2ba01477 для AT32")
    parser.add_argument("--serial", help="серийный номер пробника")
    parser.add_argument("--usb-location", help="адрес USB пробника <bus>-<port[.port]> (если серийник нечитаем)")
    parser.add_argument("--openocd", default="openocd", help="OpenOCD (на узле — путь на узле)")
    parser.add_argument("--ssh", help="user@host: выполнить OpenOCD на узле стенда")
    parser.add_argument("--ssh-key", help="ключ SSH")
    parser.add_argument("--tcl", help="host:port Tcl-порта работающего OpenOCD вместо одноразового сеанса")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("actions", nargs="+")
    args = parser.parse_args()
    actions = parse_actions(args.actions)
    if not args.tcl and not args.target:
        parser.error("нужен --target или --tcl")
    code, text = tcl(args, actions) if args.tcl else oneshot(args, actions)
    report(actions, text)
    if code and "@@0" not in text:
        print(text.strip()[-800:], file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
