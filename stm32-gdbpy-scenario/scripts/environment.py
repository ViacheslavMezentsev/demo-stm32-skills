#!/usr/bin/env python3
"""Опознание окружения до сценария: какие отладчики подключены, в каком они состоянии, к какой плате.

Навык stm32-gdbpy-scenario. Скрипт перечисляет отладчики на этой машине и (или) на узле стенда по
SSH, по желанию опрашивает каждый через OpenOCD (прошивка пробника, напряжение цели, ядро, DEV_ID)
и печатает отчёт: что подключено, чем каждый пригоден и что мешает. Пользователь выбирает нужный;
при однозначном выборе скрипт печатает аргументы для run_scenario.py и отпечаток маршрута.
Ничего не записывает в МК и не сбрасывает его: только `init`, чтение памяти и `shutdown`.

  # узел стенда: список отладчиков, без обращения к МК
  python environment.py --ssh user@stand --ssh-key ~/.ssh/id_stand

  # с опросом каждого через OpenOCD на узле
  python environment.py --ssh user@stand --ssh-key ~/.ssh/id_stand --identify --openocd '~/.local/openocd/bin/openocd'

  # эта машина и узел вместе; выбрать плату по DEV_ID и напечатать аргументы запускателя
  python environment.py --local --ssh user@stand --identify --dev-id 0x431 --emit-args --json-out env.json

Код возврата: 0 — отчёт построен (и выбор однозначен, если заданы условия выбора), 1 — подходящего
отладчика нет или подходящих несколько, 2 — ошибка запуска (SSH, OpenOCD не найден).
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

# Известные отладчики по VID:PID; "*" — любой PID производителя.
PROBES = {
    ("0483", "3744"): ("stlink", "ST-Link V1"),
    ("0483", "3748"): ("stlink", "ST-Link V2"),
    ("0483", "374a"): ("stlink", "ST-Link V2-1"),
    ("0483", "374b"): ("stlink", "ST-Link V2-1"),
    ("0483", "3752"): ("stlink", "ST-Link V2-1"),
    ("0483", "374d"): ("stlink", "STLINK-V3 (загрузчик)"),
    ("0483", "374e"): ("stlink", "STLINK-V3"),
    ("0483", "374f"): ("stlink", "STLINK-V3"),
    ("0483", "3753"): ("stlink", "STLINK-V3"),
    ("0483", "3754"): ("stlink", "STLINK-V3"),
    ("0483", "3757"): ("stlink", "STLINK-V3PWR"),
    ("1366", "*"): ("jlink", "SEGGER J-Link"),
    ("0d28", "0204"): ("cmsis-dap", "DAPLink (CMSIS-DAP)"),
    ("2e8a", "000c"): ("cmsis-dap", "Raspberry Pi Debug Probe (CMSIS-DAP)"),
    ("c251", "*"): ("cmsis-dap", "Keil ULINK (CMSIS-DAP)"),
    ("1fc9", "0090"): ("cmsis-dap", "NXP LPC-Link2 (CMSIS-DAP)"),
    ("1fc9", "0143"): ("cmsis-dap", "NXP MCU-Link (CMSIS-DAP)"),
    ("1a86", "8010"): ("wch-link", "WCH-Link"),
    ("1a86", "8012"): ("wch-link", "WCH-Link"),
    ("0403", "6010"): ("ftdi", "FTDI FT2232 (JTAG/SWD-адаптер)"),
    ("0403", "6014"): ("ftdi", "FTDI FT232H (JTAG/SWD-адаптер)"),
}
INTERFACES = {"stlink": "interface/stlink.cfg", "jlink": "interface/jlink.cfg", "cmsis-dap": "interface/cmsis-dap.cfg"}
VENDORS = sorted({vid for vid, _ in PROBES})

# DEV_ID → (серия, конфиг цели OpenOCD). Подсказка для отчёта; сверяйте с Reference Manual.
DEV_IDS = {
    0x440: ("STM32F030x8/F05x", "stm32f0x"), 0x444: ("STM32F03x", "stm32f0x"), 0x445: ("STM32F04x", "stm32f0x"),
    0x448: ("STM32F07x", "stm32f0x"), 0x442: ("STM32F030xC/F09x", "stm32f0x"),
    0x410: ("STM32F1 medium density", "stm32f1x"), 0x412: ("STM32F1 low density", "stm32f1x"),
    0x414: ("STM32F1 high density", "stm32f1x"), 0x430: ("STM32F1 XL density", "stm32f1x"),
    0x418: ("STM32F105/107", "stm32f1x"),
    0x413: ("STM32F405/407", "stm32f4x"), 0x419: ("STM32F42x/43x", "stm32f4x"), 0x423: ("STM32F401xB/C", "stm32f4x"),
    0x433: ("STM32F401xD/E", "stm32f4x"), 0x431: ("STM32F411", "stm32f4x"), 0x441: ("STM32F412", "stm32f4x"),
    0x421: ("STM32F446", "stm32f4x"), 0x434: ("STM32F469/479", "stm32f4x"),
    0x449: ("STM32F74x/75x", "stm32f7x"), 0x451: ("STM32F76x/77x", "stm32f7x"),
    0x466: ("STM32G03x/04x", "stm32g0x"), 0x460: ("STM32G07x/08x", "stm32g0x"),
    0x468: ("STM32G431/441", "stm32g4x"), 0x469: ("STM32G47x/48x", "stm32g4x"),
    0x435: ("STM32L43x/44x", "stm32l4x"), 0x415: ("STM32L47x/48x", "stm32l4x"),
    0x450: ("STM32H74x/75x", "stm32h7x"),
}
CORES = {0xC20: "Cortex-M0", 0xC60: "Cortex-M0+", 0xC23: "Cortex-M3", 0xC24: "Cortex-M4", 0xC27: "Cortex-M7",
         0xD20: "Cortex-M23", 0xD21: "Cortex-M33"}
# ST-Link V2 до этой версии JTAG-API помечается как устаревший (рекомендация обновить, не запрет).
STLINK_V2_OLD_BELOW = 28

# Перечисление USB на Linux (эта машина или узел): stdin для `python3 -`, печатает JSON.
LINUX_SCAN = r'''
import json, os
root, found = "/sys/bus/usb/devices", []
vendors = set(%r)
for entry in sorted(os.listdir(root)) if os.path.isdir(root) else []:
    base = os.path.join(root, entry)
    def read(name, raw=False):
        try:
            with open(os.path.join(base, name), "rb") as handle:
                data = handle.read().rstrip(b"\n")
            return data if raw else data.decode("utf-8", "replace")
        except OSError:
            return None
    vid = read("idVendor")
    if vid not in vendors or ":" in entry:
        continue
    bus, dev = read("busnum"), read("devnum")
    node = "/dev/bus/usb/%%03d/%%03d" %% (int(bus), int(dev)) if bus and dev else None
    drivers = sorted({os.path.basename(os.path.realpath(os.path.join(base, i, "driver")))
                      for i in os.listdir(base) if i.startswith(entry + ":") and os.path.exists(os.path.join(base, i, "driver"))})
    serial = read("serial", raw=True)
    found.append({"vid": vid, "pid": read("idProduct"), "product": read("product"), "manufacturer": read("manufacturer"),
                  "serial_hex": serial.hex() if serial is not None else None, "release": read("bcdDevice"),
                  "location": entry, "node": node, "access": bool(node and os.access(node, os.R_OK | os.W_OK)),
                  "drivers": drivers})
procs = []
for pid in os.listdir("/proc"):
    if pid.isdigit():
        try:
            with open("/proc/%%s/cmdline" %% pid, "rb") as handle:
                cmd = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        name = cmd.split(" ")[0].rsplit("/", 1)[-1]
        if name in ("openocd", "JLinkGDBServerCLExe", "JLinkGDBServer", "st-util", "pyocd"):
            procs.append({"pid": int(pid), "command": cmd[:300]})
print(json.dumps({"probes": found, "servers": procs}))
''' % (VENDORS,)

# Перечисление USB на Windows: PnP-устройства известных производителей, служба драйвера.
WINDOWS_SCAN = (
    "Get-CimInstance Win32_PnPEntity | Where-Object { $_.DeviceID -match '^USB\\\\VID_(%s)&PID_[0-9A-F]{4}\\\\' } | "
    "Select-Object DeviceID, Name, Service, Status | ConvertTo-Json -Compress" % "|".join(v.upper() for v in VENDORS))


def classify(vid, pid, product):
    kind, model = PROBES.get((vid, pid)) or PROBES.get((vid, "*")) or (None, None)
    if not kind and product and "CMSIS-DAP" in product.upper():
        kind, model = "cmsis-dap", product
    return kind or "unknown", model or (product or "%s:%s" % (vid, pid))


def serial_text(raw_hex):
    """Серийник как текст, если он печатный; иначе None (старая прошивка ST-Link V2 отдаёт байты)."""
    if not raw_hex:
        return None
    data = bytes.fromhex(raw_hex)
    return data.decode("ascii") if data and all(0x20 < b < 0x7F for b in data) else None


def run(command, timeout=60, stdin=None):
    try:
        done = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout, errors="replace")
    except FileNotFoundError as error:
        return 127, "", str(error)
    except subprocess.TimeoutExpired:
        return 124, "", "таймаут %d с" % timeout
    return done.returncode, done.stdout, done.stderr


def ssh_command(args, remote_command):
    base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    base += ["-i", os.path.expanduser(args.ssh_key)] if args.ssh_key else []
    return base + [args.ssh, remote_command]


def remote_path(path):
    """Как в run_scenario.py: ~/, $HOME/ и ${HOME}/ раскрываются на узле, остальное экранируется."""
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if path.startswith(prefix):
            return '"$HOME"/' + shlex.quote(path[len(prefix):])
    return shlex.quote(path)


def scan(args, place):
    """Список отладчиков и запущенных серверов в месте `local` или `remote`."""
    if place == "remote":
        code, out, err = run(ssh_command(args, "python3 -"), timeout=40, stdin=LINUX_SCAN)
        if code != 0:
            raise RuntimeError("узел %s: перечисление не выполнено (%s)" % (args.ssh, (err or out).strip()[-300:]))
        data = json.loads(out)
    elif os.name == "nt":
        code, out, err = run(["powershell", "-NoProfile", "-Command", WINDOWS_SCAN], timeout=60)
        items = json.loads(out) if out.strip() else []
        items = items if isinstance(items, list) else [items]
        data = {"probes": [], "servers": []}
        for item in items:
            match = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})\\(.+)$", item["DeviceID"], re.I)
            tail = match.group(3)
            # Без серийника Windows подставляет свой идентификатор с «&»: он не годится для adapter serial.
            serial = None if "&" in tail else tail
            data["probes"].append({"vid": match.group(1).lower(), "pid": match.group(2).lower(),
                                   "product": item.get("Name"), "manufacturer": None,
                                   "serial_hex": serial.encode().hex() if serial else None, "release": None,
                                   "location": None, "node": None, "access": item.get("Status") == "OK",
                                   "drivers": [item.get("Service")] if item.get("Service") else []})
    else:
        code, out, err = run([sys.executable, "-"], timeout=40, stdin=LINUX_SCAN)
        data = json.loads(out)
    probes = []
    for item in data["probes"]:
        kind, model = classify(item["vid"], item["pid"], item.get("product"))
        # Не отладчики (USB-UART и т. п. тех же производителей) в отчёт не попадают.
        if (kind == "ftdi" and not args.include_ftdi) or kind == "unknown":
            continue
        probe = dict(item, place=place, host=args.ssh if place == "remote" else None, kind=kind, model=model,
                     serial=serial_text(item.get("serial_hex")), issues=[])
        if item.get("serial_hex") and not probe["serial"]:
            probe["issues"].append("серийник нечитаем (байты вместо строки, так отвечают ST-Link V2 3748 и старые"
                                   " прошивки): выбирать по месту USB --adapter-usb-location %s"
                                   % (item.get("location") or "<bus>-<port>"))
        elif not item.get("serial_hex"):
            probe["issues"].append("серийника нет: выбирать по месту USB")
        if item.get("node") and not item.get("access"):
            probe["issues"].append("нет прав на %s: правило udev или группа plugdev" % item["node"])
        if kind == "jlink" and any(d and d.lower() == "winusb" for d in item.get("drivers") or []):
            probe["issues"].append("драйвер WinUSB: SEGGER J-Link Software пробник не видит, OpenOCD — видит")
        if kind == "wch-link":
            probe["issues"].append("навык не поддерживает этот тип напрямую")
        probes.append(probe)
    return probes, data.get("servers", [])


def select_args(probe):
    if probe["serial"]:
        return ["-c", "adapter serial %s" % probe["serial"]]
    if probe.get("location"):
        return ["-c", "adapter usb location %s" % probe["location"]]
    return []


def identify_command(probe, openocd):
    """Команда OpenOCD: только init, чтение CPUID и IDCODE, shutdown. Без сброса и записи."""
    command = [openocd, "-f", INTERFACES[probe["kind"]]] + select_args(probe)
    if probe["kind"] != "stlink":
        command += ["-c", "transport select swd"]
    # CPUTAPID 0 — не сверять код DAP: конфиг цели общий, а плата заранее неизвестна.
    command += ["-c", "set CPUTAPID 0", "-f", "target/stm32f4x.cfg", "-c", "init",
                "-c", "mdw 0xE000ED00",
                "-c", "set p [expr {([lindex [read_memory 0xE000ED00 32 1] 0] >> 4) & 0xFFF}]; "
                      "if {$p == 0xC20 || $p == 0xC60} {mdw 0x40015800} else {mdw 0xE0042000}",
                "-c", "shutdown"]
    return command


def parse_identify(text):
    """Сведения из журнала OpenOCD: прошивка пробника, напряжение, ядро, IDCODE, причина отказа."""
    info = {}
    stlink = re.search(r"STLINK (V\d+J\d+\S*) \(API v(\d+)\) VID:PID ([0-9A-F]{4}):([0-9A-F]{4})", text)
    if stlink:
        info["firmware"] = stlink.group(1)
        jtag = re.match(r"V(\d+)J(\d+)", stlink.group(1))
        info["stlink_version"], info["stlink_jtag"] = int(jtag.group(1)), int(jtag.group(2))
    for pattern in (r"Firmware: (.+)", r"FW Version = (.+)", r"(J-Link .+ compiled .+)"):
        found = re.search(pattern, text)
        if found and "firmware" not in info:
            info["firmware"] = found.group(1).strip()
    voltage = re.search(r"(?:Target voltage|VTarget)\s*[:=]\s*([\d.]+)", text)
    if voltage:
        info["voltage"] = float(voltage.group(1))
    words = dict((int(a, 16), int(v, 16)) for a, v in re.findall(r"0x([0-9a-f]{8}):\s+([0-9a-f]{8})", text, re.I))
    if 0xE000ED00 in words:
        part = (words[0xE000ED00] >> 4) & 0xFFF
        info["cpuid"], info["core"] = "0x%08X" % words[0xE000ED00], CORES.get(part, "PARTNO 0x%03X" % part)
    for address in (0xE0042000, 0x40015800):
        if address in words and words[address]:
            dev = words[address] & 0xFFF
            info["idcode"], info["dev_id"] = "0x%08X" % words[address], "0x%03X" % dev
            info["family"], info["target"] = DEV_IDS.get(dev, (None, None))
            if not info["family"]:
                info["family"] = "серия не опознана: сверить IDCODE с документацией производителя"
    for pattern, reason in ((r"LIBUSB_ERROR_ACCESS|permission", "нет прав доступа к USB"),
                            (r"LIBUSB_ERROR_BUSY|resource busy|in use", "пробник занят другим процессом"),
                            (r"open failed|unable to open|No J-Link device found|unable to find", "пробник не открылся"),
                            (r"init mode failed|cannot read IDR|Error connecting DP|target voltage may be too low",
                             "нет связи с целью (питание, провода SWD, режим сна)")):
        if "dev_id" not in info and re.search(pattern, text, re.I):
            info["error"] = reason
            break
    return info


def identify(args, probe):
    if probe["kind"] not in INTERFACES:
        return
    if probe["place"] == "remote":
        command = identify_command(probe, "OPENOCD")
        remote = " ".join([remote_path(args.openocd_remote)] + [shlex.quote(part) for part in command[1:]])
        code, out, err = run(ssh_command(args, remote), timeout=args.timeout)
    else:
        code, out, err = run(identify_command(probe, args.openocd), timeout=args.timeout)
    info = parse_identify(out + err)
    probe.update(info)
    # Хвост журнала OpenOCD — в JSON: по нему разбирают отказ без повторного опроса.
    probe["log_tail"] = (out + err).strip().splitlines()[-15:]
    if "error" not in info and "dev_id" not in info:
        probe["error"] = "МК не опознан: " + ((err or out).strip().splitlines() or ["нет вывода"])[-1][:200]
    if probe.get("stlink_version") == 2 and probe.get("stlink_jtag", 99) < STLINK_V2_OLD_BELOW:
        probe["issues"].append("прошивка %s устарела: обновите через STM32CubeProgrammer (ST-LinkUpgrade)"
                               % probe["firmware"])
    if probe.get("voltage") is not None and probe["voltage"] < 1.5:
        probe["issues"].append("напряжение цели %.2f В: плата не запитана или не подключена" % probe["voltage"])


def fingerprint(probe):
    who = probe["serial"] or ("usb:" + (probe.get("location") or "?"))
    place = "remote:%s" % probe["host"] if probe["place"] == "remote" else "local"
    return "|".join([place, probe["kind"], who, probe.get("firmware") or "-", probe.get("dev_id") or "-"])


def runner_args(args, probe):
    """Аргументы run_scenario.py для выбранного отладчика."""
    out = []
    if probe["place"] == "remote":
        out += ["--ssh", probe["host"]] + (["--ssh-key", args.ssh_key] if args.ssh_key else [])
        out += ["--openocd", args.openocd_remote]
    out += ["--openocd-config", INTERFACES.get(probe["kind"], "interface/<?>.cfg")]
    if probe["kind"] in ("jlink", "cmsis-dap"):
        out += ["--openocd-transport", "swd"]
    out += ["--openocd-config", "target/%s.cfg" % (probe.get("target") or "<серия>")]
    out += ["--adapter-serial", probe["serial"]] if probe["serial"] else \
        ["--adapter-usb-location", probe.get("location") or "<bus>-<port>"]
    return out


def matches(args, probe):
    if args.serial and probe["serial"] != args.serial:
        return False
    if args.usb_location and probe.get("location") != args.usb_location:
        return False
    if args.kind and probe["kind"] != args.kind:
        return False
    if args.dev_id and probe.get("dev_id") != "0x%03X" % int(args.dev_id, 16):
        return False
    return True


def report(probes, servers, chosen, args):
    places = sorted({(p["place"], p["host"]) for p in probes} | ({("remote", args.ssh)} if args.ssh else set())
                    | ({("local", None)} if args.local else set()))
    for place, host in places:
        group = [p for p in probes if (p["place"], p["host"]) == (place, host)]
        print("%s: отладчиков %d" % ("узел " + host if place == "remote" else "эта машина", len(group)))
        for number, probe in enumerate(group, 1):
            mark = "*" if probe is chosen else " "
            who = probe["serial"] or "серийник нечитаем" if probe.get("serial_hex") else probe["serial"] or "без серийника"
            parts = ["%s%d. %s" % (mark, number, probe["model"]), "%s:%s" % (probe["vid"], probe["pid"]), who]
            parts += ["USB %s" % probe["location"]] if probe.get("location") else []
            parts += ["прошивка %s" % probe["firmware"]] if probe.get("firmware") else []
            parts += ["%.2f В" % probe["voltage"]] if probe.get("voltage") is not None else []
            if probe.get("dev_id"):
                parts.append("%s DEV_ID %s %s" % (probe.get("core") or "", probe["dev_id"], probe.get("family") or ""))
            elif probe.get("core"):
                parts.append(probe["core"])
            print("  " + " | ".join(part.strip() for part in parts if part.strip()))
            for line in ([probe["error"]] if probe.get("error") else []) + probe["issues"]:
                print("       ! " + line)
        busy = [s for s in servers if (place == "remote") == bool(s.get("remote"))]
        for server in busy:
            print("  запущен сервер (может держать пробник): pid %d %s" % (server["pid"], server["command"][:120]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", action="store_true", help="искать на этой машине")
    parser.add_argument("--ssh", help="user@host узла стенда")
    parser.add_argument("--ssh-key", help="ключ SSH")
    parser.add_argument("--identify", action="store_true", help="опросить каждый отладчик через OpenOCD (без записи)")
    parser.add_argument("--openocd", default="openocd", help="OpenOCD на этой машине")
    parser.add_argument("--openocd-remote", default="openocd", help="OpenOCD на узле: абсолютный путь, ~/ или $HOME/")
    parser.add_argument("--timeout", type=int, default=40, help="бюджет одного опроса OpenOCD, с")
    parser.add_argument("--include-ftdi", action="store_true", help="показывать FTDI-адаптеры")
    parser.add_argument("--serial", help="выбрать по серийнику")
    parser.add_argument("--usb-location", help="выбрать по месту USB <bus>-<port>")
    parser.add_argument("--kind", choices=("stlink", "jlink", "cmsis-dap"), help="выбрать по типу")
    parser.add_argument("--dev-id", help="выбрать по DEV_ID, например 0x431 (нужен --identify)")
    parser.add_argument("--emit-args", action="store_true", help="напечатать аргументы run_scenario.py для выбранного")
    parser.add_argument("--json-out", help="записать отчёт в JSON")
    args = parser.parse_args()
    if not (args.local or args.ssh):
        args.local = True
    if args.dev_id and not args.identify:
        parser.error("--dev-id требует --identify")
    probes, servers = [], []
    try:
        for place in (["local"] if args.local else []) + (["remote"] if args.ssh else []):
            found, running = scan(args, place)
            probes += found
            servers += [dict(s, remote=place == "remote") for s in running]
        if args.identify:
            if args.local and not shutil.which(args.openocd) and not os.path.isfile(args.openocd):
                raise RuntimeError("OpenOCD не найден на этой машине: %s" % args.openocd)
            if args.ssh:
                code, out, _ = run(ssh_command(args, "command -v %s >/dev/null 2>&1 && echo FOUND"
                                                      % remote_path(args.openocd_remote)), timeout=30)
                if "FOUND" not in out:
                    raise RuntimeError("OpenOCD не найден на узле %s: %s" % (args.ssh, args.openocd_remote))
            for probe in probes:
                identify(args, probe)
    except (RuntimeError, ValueError) as error:
        print("ОШИБКА: %s" % error, file=sys.stderr)
        return 2
    for probe in probes:
        probe["fingerprint"] = fingerprint(probe)
    selecting = any((args.serial, args.usb_location, args.kind, args.dev_id))
    candidates = [p for p in probes if matches(args, p) and not p.get("error")] if selecting else []
    chosen = candidates[0] if len(candidates) == 1 else None
    report(probes, servers, chosen, args)
    result = {"probes": probes, "servers": servers, "selected": chosen and chosen["fingerprint"]}
    code = 0
    if selecting and not chosen:
        print("выбор: %s — уточните --serial, --usb-location, --kind или --dev-id"
              % ("подходящих нет" if not candidates else "подходят %d" % len(candidates)))
        code = 1
    elif chosen:
        print("выбор: %s" % chosen["fingerprint"])
        result["runner_args"] = runner_args(args, chosen)
        if args.emit_args:
            print("аргументы run_scenario.py: " + " ".join(shlex.quote(a) for a in result["runner_args"]))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    return code


if __name__ == "__main__":
    sys.exit(main())
