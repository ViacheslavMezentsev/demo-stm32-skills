#!/usr/bin/env python3
"""Checks of the skill kits beyond the SKILL.md format (skills-ref covers that).

    python3 ci/run_checks.py kits                 # host only: scripts, assets, self-tests
    python3 ci/run_checks.py emulation            # pinned GCC + Renode 1.17 (+ QEMU if present)
    python3 ci/run_checks.py kits emulation --strict

kits       Python scripts compile, shell scripts parse, JSON/JSONC and TOML assets load,
           .clang-format assets are accepted by clang-format >= 16, embedded-tech-spec
           checks its own template and example, renode_compat converts the mock platform
           both ways, weact-logic-analyzer self-test runs on synthetic signals (sigrok-cli).
emulation  The CI firmware (tests/firmware/boot-g474) is built with the toolchain file of
           stm32-vscode-cmake-scaffold, run in Renode 1.17 by stm32-renode-emulator and its
           boot log verified; the same ELF runs in QEMU (profile F411) when qemu-system-arm
           is available. The mock platform is checked against the STM32G474 SVD through
           svd_lookup of stm32-ros-alt-channel, whose resolve_addresses.py then resolves the
           firmware fields from the ELF; the resolved RAM addresses are read twice from the
           running core in Renode and must show a changing counter.

A missing optional tool (sigrok-cli, clang-format, qemu-system-arm, GDB with Python) gives
SKIP; with --strict it is a failure. Results: build/ci/summary.json, logs: build/ci/*.log.
Emulator results prove behaviour in the emulator only, not on hardware.
"""
import argparse
import json
import os
import pathlib
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "ci"
FIRMWARE = ROOT / "tests" / "firmware" / "boot-g474"
RENODE_SKILL = ROOT / "stm32-renode-emulator"
ROS_SKILL = ROOT / "stm32-ros-alt-channel"

# Renode mock peripherals and their SVD names (dwt is a core block, not in the device SVD).
MOCK_TO_SVD = {
    "rcc": "RCC", "syscfg": "SYSCFG", "flash_ctrl": "FLASH", "gpioa": "GPIOA", "gpiob": "GPIOB",
    "gpioc": "GPIOC", "dbgmcu": "DBGMCU", "pwr": "PWR", "crc": "CRC", "tim1": "TIM1",
    "exti": "EXTI", "usart1": "USART1", "usart2": "USART2", "usart3": "USART3", "adc": "ADC1",
    "fdcan1": "FDCAN1", "rtc": "RTC", "dma1": "DMA1", "dma2": "DMA2",
}


class Skip(Exception):
    """Optional tool is missing."""


class Runner(object):
    def __init__(self, strict):
        self.strict = strict
        self.results = []

    def check(self, name, function):
        started = time.monotonic()
        try:
            details = function() or ""
            status = "PASS"
        except Skip as reason:
            status, details = ("FAIL" if self.strict else "SKIP"), str(reason)
        except Exception as error:  # report every failed check, keep going
            status, details = "FAIL", "%s: %s" % (type(error).__name__, error)
        elapsed = time.monotonic() - started
        self.results.append({"check": name, "status": status, "details": details,
                             "seconds": round(elapsed, 1)})
        print("%-4s %-44s %s" % (status, name, details), flush=True)


def run(argv, log=None, timeout=300, **kwargs):
    """Run a command; raise with the log tail on a non-zero exit."""
    result = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                            errors="replace", timeout=timeout, **kwargs)
    text = result.stdout + result.stderr
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(text, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError("exit %d: %s" % (result.returncode, text.strip()[-600:]))
    return text


def tracked(pattern):
    text = subprocess.run(["git", "ls-files", pattern], cwd=ROOT, capture_output=True,
                          text=True).stdout
    return [ROOT / line for line in text.splitlines() if line]


# --------------------------------------------------------------------------- kits

def check_python():
    files = tracked("*.py")
    with tempfile.TemporaryDirectory() as temp:
        for index, path in enumerate(files):
            py_compile.compile(str(path), cfile=os.path.join(temp, "%d.pyc" % index), doraise=True)
    return "%d files" % len(files)


def check_shell():
    files = tracked("*.sh")
    for path in files:
        run(["bash", "-n", path])
    return "%d files" % len(files)


def strip_jsonc(text):
    """JSON with comments and trailing commas (VS Code) -> plain JSON."""
    out, i, quoted = [], 0, False
    while i < len(text):
        char = text[i]
        if quoted:
            out.append(char)
            if char == "\\":
                out.append(text[i + 1])
                i += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            out.append(char)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
            continue
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
            continue
        else:
            out.append(char)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def check_json():
    files = tracked("*.json")
    for path in files:
        text = path.read_text(encoding="utf-8-sig")
        try:
            json.loads(text)
        except ValueError:
            json.loads(strip_jsonc(text))  # VS Code files are JSONC
    return "%d files" % len(files)


def check_toml():
    try:
        import tomllib
    except ModuleNotFoundError:
        raise Skip("Python < 3.11 (tomllib)")
    files = tracked("*.toml")
    for path in files:
        tomllib.loads(path.read_text(encoding="utf-8"))
    return "%d files" % len(files)


def check_clang_format():
    tool = shutil.which("clang-format")
    if not tool:
        raise Skip("clang-format not found")
    version = re.search(r"version (\d+)", run([tool, "--version"]))
    if not version or int(version.group(1)) < 16:
        raise Skip("clang-format >= 16 needed (SeparateDefinitionBlocks)")
    files = tracked("*clang-format")
    for path in files:
        run([tool, "--style=file:%s" % path, "--dump-config"])
    return "%d files, clang-format %s" % (len(files), version.group(1))


def check_tech_spec():
    script = ROOT / "embedded-tech-spec" / "scripts" / "check_spec.py"
    report = []
    for doc in ("assets/template.md", "references/example-eeprom.md"):
        text = run([sys.executable, "-B", script, ROOT / "embedded-tech-spec" / doc],
                   log=OUT / ("check_spec-%s.log" % pathlib.Path(doc).stem))
        errors = re.search(r"Ошибок: (\d+)", text)
        if not errors or errors.group(1) != "0":
            raise RuntimeError("%s: %s" % (doc, text.strip().splitlines()[-1]))
        report.append(pathlib.Path(doc).name)
    return "no errors: " + ", ".join(report)


def check_renode_compat():
    sys.path.insert(0, str(RENODE_SKILL / "scripts"))
    import renode_compat as compat
    mock = (RENODE_SKILL / "assets" / "stm32g4_mock.repl").read_text(encoding="utf-8")
    old = compat.platform_text(mock, (1, 16, 1))
    new = compat.platform_text(old, (1, 17, 0))
    assert compat.OLD_CPU_UART in old and compat.NEW_CPU_UART not in old, "1.16.1 form"
    assert new == mock, "1.17 form must round-trip to the shipped platform"
    assert compat.uart_path((1, 16, 1)) == "sysbus.cpu.uart"
    assert compat.uart_path((1, 17, 0)) == "cpu.semihosting.uart"
    assert compat.parse_version("Renode v1.17.0\n  build: 1.17.0+2026") == (1, 17, 0)
    return "platform 1.17 <-> 1.16.1, uart path, version parsing"


def check_weact():
    if not shutil.which("sigrok-cli"):
        raise Skip("sigrok-cli not found")
    script = ROOT / "weact-logic-analyzer" / "scripts" / "test_weact_la.py"
    text = run([sys.executable, "-B", script], log=OUT / "weact.log", timeout=180,
               cwd=script.parent)
    return text.strip().splitlines()[-1] if text.strip() else "ok"


KITS = [
    ("kits: python scripts compile", check_python),
    ("kits: shell scripts parse", check_shell),
    ("kits: JSON/JSONC files load", check_json),
    ("kits: TOML files load", check_toml),
    ("kits: .clang-format assets", check_clang_format),
    ("embedded-tech-spec: template and example", check_tech_spec),
    ("stm32-renode-emulator: renode_compat", check_renode_compat),
    ("weact-logic-analyzer: self-test", check_weact),
]


# --------------------------------------------------------------------- emulation

class Emulation(object):
    def __init__(self, tools):
        self.tools = tools
        self.gcc = tools / "gcc" / "bin"
        self.renode = tools / "renode" / "renode"
        self.svd = tools / "svd-stm32g474" / "STM32G474.svd"
        self.build = OUT / "boot-g474"
        self.elf = self.build / "firmware.elf"
        self.env = dict(os.environ, PATH=str(self.gcc) + os.pathsep + os.environ.get("PATH", ""),
                        STM32_SVD=str(self.svd), STM32_TOOLCHAIN_PATH=str(tools / "gcc"))

    def fetch(self):
        text = run([sys.executable, "-B", ROOT / "ci" / "fetch_tools.py", "--dest", self.tools],
                   log=OUT / "fetch_tools.log", timeout=900)
        return "; ".join(line.split(None, 1)[0] for line in text.splitlines() if line.strip())

    def firmware(self):
        if self.build.exists():
            shutil.rmtree(self.build)
        toolchain = ROOT / "stm32-vscode-cmake-scaffold" / "assets" / "gcc-arm-none-eabi.cmake"
        generator = ["-G", "Ninja"] if shutil.which("ninja") else []
        run(["cmake", "-S", FIRMWARE, "-B", self.build] + generator +
            ["-DCMAKE_TOOLCHAIN_FILE=%s" % toolchain, "-DSTM32_TOOLCHAIN_PATH=%s" % (self.tools / "gcc")],
            log=OUT / "firmware-configure.log", env=self.env)
        text = run(["cmake", "--build", self.build], log=OUT / "firmware-build.log", env=self.env)
        size = re.search(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+\d+", text, re.M)
        return "text %s, data %s, bss %s bytes" % size.groups() if size else "built"

    def emulator_run(self, profile, expected, binary_args):
        output = OUT / ("run-%s" % profile.lower())
        if output.exists():
            shutil.rmtree(output)
        run([sys.executable, "-B", RENODE_SKILL / "scripts" / "run.py", "--profile", profile,
             "--elf", self.elf, "--output", output, "--verify", "--expected", expected,
             "--objcopy", self.gcc / "arm-none-eabi-objcopy"] + binary_args,
            log=OUT / ("run-%s.log" % profile.lower()), env=self.env, timeout=240)
        result = json.loads((output / "run.json").read_text(encoding="utf-8"))
        if result.get("status") != "PASS":
            raise RuntimeError("run.json status %s" % result.get("status"))
        return result

    def renode_boot(self):
        result = self.emulator_run("G474", FIRMWARE / "expected.toml", ["--renode", self.renode])
        return "boot log PASS in Renode %s" % result.get("renode_version")

    def negative_log(self):
        """The checker must reject a truncated log (otherwise PASS above proves nothing)."""
        log = OUT / "run-g474" / "startup.log"
        lines = log.read_text(encoding="utf-8").splitlines()
        broken = OUT / "negative" / "startup.log"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", str(RENODE_SKILL / "scripts" / "check_startup_log.py"),
             "--log", str(broken), "--elf", str(self.elf), "--expected", str(FIRMWARE / "expected.toml"),
             "--objcopy", str(self.gcc / "arm-none-eabi-objcopy")], capture_output=True, text=True)
        if result.returncode != 1:
            raise RuntimeError("truncated log accepted (exit %d)" % result.returncode)
        return "truncated log rejected"

    def qemu_boot(self):
        qemu = shutil.which("qemu-system-arm")
        if not qemu:
            raise Skip("qemu-system-arm not found")
        expected = OUT / "expected-qemu.toml"
        text = (FIRMWARE / "expected.toml").read_text(encoding="utf-8")
        expected.write_text(text.replace('emulator = "renode"', 'emulator = "qemu"')
                            .replace('machine = "stm32g4_mock"', 'machine = "netduinoplus2"'),
                            encoding="utf-8")
        self.emulator_run("F411", expected, ["--qemu", qemu])
        return "same ELF, boot log PASS in QEMU netduinoplus2"

    def mock_vs_svd(self):
        sys.path.insert(0, str(ROS_SKILL / "scripts"))
        from svd_lookup import SvdLookup
        svd = SvdLookup(self.svd)
        mock = (RENODE_SKILL / "assets" / "stm32g4_mock.repl").read_text(encoding="utf-8")
        found = dict(re.findall(r"^(\w+): Memory\.MappedMemory @ sysbus (0x[0-9A-Fa-f]+)", mock, re.M))
        wrong = []
        for name, peripheral in MOCK_TO_SVD.items():
            if name not in found:
                wrong.append("%s: not in mock" % name)
                continue
            base = svd.peripherals.get(peripheral, {}).get("base")
            if base != int(found[name], 16):
                wrong.append("%s=%s, SVD %s=%s" % (name, found[name], peripheral,
                                                   hex(base) if base is not None else "missing"))
        if wrong:
            raise RuntimeError("; ".join(wrong))
        return "%d stub addresses match %s" % (len(MOCK_TO_SVD), self.svd.name)

    def gdb(self):
        names = ("arm-none-eabi-gdb-py3", "gdb-multiarch", "arm-none-eabi-gdb")
        for name in names:
            path = shutil.which(name, path=self.env["PATH"])
            if path and subprocess.run([path, "-q", "-batch", "-ex", "python print(42)"],
                                       capture_output=True, text=True).stdout.strip() == "42":
                return path
        raise Skip("no GDB with Python (%s)" % ", ".join(names))

    def resolve(self, extra, name):
        out = OUT / ("addresses-%s.json" % name)
        env = dict(self.env, GDB=self.gdb())
        run([sys.executable, "-B", ROS_SKILL / "scripts" / "resolve_addresses.py", "--elf", self.elf,
             "--spec", FIRMWARE / "fields.toml", "--out", out] + extra,
            log=OUT / ("resolve-%s.log" % name), env=env, timeout=120)
        fields = {item["expr"]: item["address"] for item in
                  json.loads(out.read_text(encoding="utf-8"))["fields"]}
        symbols = {}
        for line in run([self.gcc / "arm-none-eabi-nm", self.elf]).splitlines():
            parts = line.split()
            if len(parts) == 3:
                symbols[parts[2]] = int(parts[0], 16)
        for symbol in ("telemetry_counter", "telemetry_state"):
            if fields["&" + symbol] != symbols[symbol]:
                raise RuntimeError("%s: %#x, nm %#x" % (symbol, fields["&" + symbol], symbols[symbol]))
        if fields["svd:RCC.RCC_CR.HSION"] != 0x40021000 or fields["svd:GPIOA.ODR"] != 0x48000014:
            raise RuntimeError("SVD fields: %s" % fields)
        return fields

    def resolve_offline(self):
        self.resolve([], "offline")
        return "ELF symbols match nm, SVD registers resolved"

    def live(self):
        """Read the resolved RAM addresses twice from the running emulated core.

        The addresses come from resolve_addresses.py (ELF/DWARF), the reads from the Renode
        monitor (sysbus ReadDoubleWord) between two RunFor slices: the same idea as route A
        of stm32-ros-alt-channel (reading RAM while the firmware runs, no firmware changes),
        with Renode in place of OpenOCD. The Renode GDB server is not used here: it needs an
        IPv6-capable loopback, which some CI sandboxes lack.
        """
        fields = self.resolve([], "live")
        counter, state = fields["&telemetry_counter"], fields["&telemetry_state"]
        work = OUT / "live"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        platform = work / "platform.repl"
        shutil.copy2(RENODE_SKILL / "assets" / "stm32g4_mock.repl", platform)
        script = work / "live.resc"
        script.write_text("\n".join([
            "logLevel 3", "using sysbus", 'mach create "board"',
            "machine LoadPlatformDescription @%s" % platform.as_posix(),
            "cpu.semihosting.uart CreateFileBackend @%s true" % (work / "startup.log").as_posix(),
            "sysbus LoadELF @%s" % self.elf.as_posix(),
            'emulation RunFor "0.2"', 'echo "A="', "sysbus ReadDoubleWord %#x" % counter,
            'emulation RunFor "0.2"', 'echo "B="', "sysbus ReadDoubleWord %#x" % counter,
            'echo "S="', "sysbus ReadDoubleWord %#x" % state, "quit", ""]), encoding="utf-8")
        text = run([self.renode, "--disable-gui", "--console", "--plain",
                    "-e", "include @%s" % script.as_posix()], log=work / "renode.log", timeout=120)
        values = dict(re.findall(r"^([ABS])=\s*\n(0x[0-9A-Fa-f]+)", text, re.M))
        if set(values) != {"A", "B", "S"}:
            raise RuntimeError("unexpected monitor output: %s" % text.strip()[-300:])
        a, b, s = (int(values[key], 16) for key in "ABS")
        if not 0 < a < b or s != 0xA5:
            raise RuntimeError("counter %d -> %d, state %#x" % (a, b, s))
        return "telemetry_counter %d -> %d at %#x, telemetry_state %#x at %#x" % (a, b, counter, s, state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stages", nargs="+", choices=["kits", "emulation"])
    parser.add_argument("--strict", action="store_true", help="SKIP counts as failure")
    parser.add_argument("--tools", type=pathlib.Path, default=ROOT / "build" / "ci-tools",
                        help="directory for ci/fetch_tools.py downloads")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    runner = Runner(args.strict)
    if "kits" in args.stages:
        for name, function in KITS:
            runner.check(name, function)
    if "emulation" in args.stages:
        emulation = Emulation(args.tools.resolve())
        steps = [
            ("emulation: fetch pinned tools", emulation.fetch),
            ("stm32-vscode-cmake-scaffold: build firmware", emulation.firmware),
            ("stm32-renode-emulator: Renode G474 boot", emulation.renode_boot),
            ("stm32-renode-emulator: truncated log fails", emulation.negative_log),
            ("stm32-renode-emulator: QEMU F411 boot", emulation.qemu_boot),
            ("renode mock vs SVD (svd_lookup)", emulation.mock_vs_svd),
            ("stm32-ros-alt-channel: resolve offline", emulation.resolve_offline),
            ("stm32-ros-alt-channel: read RAM in Renode", emulation.live),
        ]
        for name, function in steps:
            runner.check(name, function)
            if name.startswith(("emulation: fetch", "stm32-vscode")) and runner.results[-1]["status"] != "PASS":
                break  # nothing to run without tools or firmware
    summary = {"schema": 1, "stages": args.stages, "strict": args.strict, "checks": runner.results}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
    failed = [item for item in runner.results if item["status"] == "FAIL"]
    print("\n%d checks, %d failed, %d skipped -> %s" % (
        len(runner.results), len(failed),
        sum(item["status"] == "SKIP" for item in runner.results), OUT / "summary.json"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
