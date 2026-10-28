#!/usr/bin/env python3
"""Короткие команды цели через SEGGER J-Link Commander (JLinkExe): локально или по SSH.

Навык stm32-gdbpy-scenario. Те же действия, что у ocd.py, но без OpenOCD: каждый вызов
запускает JLinkExe с файлом команд (подключение → команды → выход). Подключение
Commander не останавливает ядро, чтение памяти идёт во время работы.

  python jlink.py --device AT32F403ACGU7 --serial <номер J-Link> state read 0xE0042000
  python jlink.py --ssh user@stand --jlinkexe /opt/SEGGER/JLink/JLinkExe --device STM32F103C8 \\
      --serial <номер> halt state resume state

Действия: state, read ADDR [N], halt, resume, reset-run, reset-halt, raw "команда JLinkExe".
Код возврата — код JLinkExe (с -ExitOnError 1 ошибка команды даёт 1).
"""
import argparse
import re
import shlex
import subprocess
import sys

DHCSR = 0xE000EDF0
ACTIONS = {"state": 0, "read": None, "halt": 0, "resume": 0, "reset-run": 0, "reset-halt": 0, "raw": 1}
PROMPT = "J-Link>"
WORDS = re.compile(r"^([0-9A-Fa-f]{8}) = ((?:[0-9A-Fa-f]{8}\s*)+)$")


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


def commands(actions):
    """По одной группе команд JLinkExe на действие; группы разделяются пустой командой sleep 0."""
    groups = []
    for name, args in actions:
        if name == "state":
            groups.append(["mem32 %08X, 1" % DHCSR])
        elif name == "read":
            groups.append(["mem32 %08X, %d" % (int(args[0], 0), int(args[1], 0) if len(args) > 1 else 1)])
        elif name == "halt":
            groups.append(["h"])
        elif name == "resume":
            groups.append(["g"])
        elif name == "reset-run":
            # r сбрасывает и останавливает ядро; g запускает прошивку с начала.
            groups.append(["r", "g"])
        elif name == "reset-halt":
            groups.append(["r"])
        else:
            groups.append([args[0]])
    lines = []
    for group in groups:
        lines += ["sleep 0"] + group
    return lines + ["sleep 0", "q"]


def decode_state(value):
    if value & (1 << 19):
        return "LOCKUP"
    if value & (1 << 17):
        return "HALTED"
    if value & (1 << 18):
        return "SLEEPING"
    return "RUNNING"


def split(text):
    """Ответы по группам: границы — эхо «J-Link>sleep 0» в выводе Commander."""
    chunks, current = [], None
    for line in text.splitlines():
        if line.startswith(PROMPT):
            if line[len(PROMPT):].strip().lower() == "sleep 0":
                if current is not None:
                    chunks.append(current)
                current = []
                continue
        if current is not None:
            current.append(line)
    return chunks


def words(lines):
    rows = []
    for line in lines:
        match = WORDS.match(line.strip())
        if match:
            address = int(match.group(1), 16)
            rows += [(address + 4 * i, int(item, 16)) for i, item in enumerate(match.group(2).split())]
    return rows


def report(actions, text):
    chunks = split(text)
    for number, (name, _) in enumerate(actions):
        body = chunks[number] if number < len(chunks) else []
        errors = [line.strip() for line in body if "error" in line.lower() or "failed" in line.lower()]
        if name == "state":
            rows = words(body)
            print("state %s (DHCSR 0x%08X)" % (decode_state(rows[0][1]), rows[0][1]) if rows else "state ?")
        elif name == "read":
            for address, value in words(body):
                print("0x%08X 0x%08X" % (address, value))
        elif name == "raw":
            print("\n".join(line for line in body if not line.startswith(PROMPT)).strip())
        elif name == "resume" and any("not halted" in line.lower() for line in errors):
            # Commander при выходе отпускает ядро: «не остановлено» означает, что оно уже работает.
            print("resume ok (already running)")
            continue
        else:
            print(name + (" ERROR" if errors else " ok"))
        for line in errors:
            print("  " + line)


def remote_path(path):
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def run(args, actions):
    script = "\n".join(commands(actions)) + "\n"
    options = ["-device", args.device, "-if", args.interface, "-speed", str(args.speed), "-autoconnect", "1",
               "-NoGui", "1", "-ExitOnError", "1"]
    options += ["-USB", args.serial] if args.serial else []
    if args.ssh:
        # Файл команд создаётся и удаляется на узле в том же вызове SSH.
        remote = ("f=$(mktemp); printf %s > $f; " % shlex.quote(script) + remote_path(args.jlinkexe) + " "
                  + " ".join(shlex.quote(o) for o in options) + " -CommandFile $f 2>&1; rc=$?; rm -f $f; exit $rc")
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        command += (["-i", args.ssh_key] if args.ssh_key else []) + [args.ssh, remote]
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=args.timeout, errors="replace")
    else:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".jlink", delete=False) as handle:
            handle.write(script)
        try:
            proc = subprocess.run([args.jlinkexe] + options + ["-CommandFile", handle.name], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, timeout=args.timeout, errors="replace")
        finally:
            import os
            os.unlink(handle.name)
    return proc.returncode, proc.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", required=True, help="имя МК в базе J-Link (STM32F103C8, AT32F403ACGU7, ...)")
    parser.add_argument("--serial", help="серийный номер J-Link")
    parser.add_argument("--interface", default="SWD", choices=("SWD", "JTAG"))
    parser.add_argument("--speed", type=int, default=1000, help="кГц")
    parser.add_argument("--jlinkexe", default="JLinkExe" if sys.platform != "win32" else "JLink.exe",
                        help="J-Link Commander (на узле — путь на узле)")
    parser.add_argument("--ssh", help="user@host: выполнить на узле стенда")
    parser.add_argument("--ssh-key")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("actions", nargs="+")
    args = parser.parse_args()
    actions = parse_actions(args.actions)
    code, text = run(args, actions)
    report(actions, text)
    if code:
        print(text.strip()[-800:], file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
