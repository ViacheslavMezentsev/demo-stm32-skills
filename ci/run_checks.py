#!/usr/bin/env python3
"""Checks of the skill kits beyond the SKILL.md format (skills-ref covers that).

    python3 ci/run_checks.py kits                 # host only: scripts, assets, self-tests
    python3 ci/run_checks.py emulation            # pinned GCC + Renode 1.17 (+ QEMU if present)
    python3 ci/run_checks.py kits emulation --strict

kits       Python scripts compile, shell scripts parse, JSON/JSONC and TOML assets load,
           .clang-format assets are accepted by clang-format >= 16, embedded-tech-spec
           checks its own template and example, renode_compat converts the mock platform
           both ways, weact-logic-analyzer self-test runs on synthetic signals (sigrok-cli),
           OpenOCD offers the RTT commands that stm32-rtt-console uses.
emulation  The probe firmware (stm32-skills-probe, pinned commit) is built for F411CE, F401CC
           and F103C8 with semihosting and a timed exit; F411CE uses the toolchain file of
           stm32-vscode-cmake-scaffold. stm32-renode-emulator runs every build in Renode 1.17
           on the probe model and the F4 builds in QEMU, and verifies the boot log against the
           probe's expected files. The renode-emulator mock and the probe model are checked
           against the device SVDs through svd_lookup of stm32-ros-alt-channel, whose
           resolve_addresses.py resolves probe symbols and a struct field from the ELF; the
           resolved RAM addresses are read twice from the running core in Renode.

A missing optional tool (sigrok-cli, clang-format, openocd, qemu-system-arm, GDB with Python) gives
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
BOARDS = ("F411CE", "F401CC", "F103C8")
PROBE_MODELS = {"F411CE": "stm32f4_probe.repl", "F401CC": "stm32f4_probe.repl", "F103C8": "stm32f103_probe.repl"}
PROBE_EXIT_MS = 500  # the probe ends the run itself: QEMU terminates, Renode halts the core
SEMIHOSTING_UART = "semihosting: CPU.SemihostingHandler @ cpu\nuart: UART.SemihostingUart @ semihosting\n"
# Probe model peripherals (F4) and their names in the STM32F411 SVD.
PROBE_MODEL_TO_SVD = {"usart1": "USART1", "tim1": "TIM1", "gpioa": "GPIOA", "gpiob": "GPIOB", "gpioc": "GPIOC",
                      "rcc": "RCC", "flash_ctrl": "FLASH"}
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


def check_openocd_rtt():
    tool = shutil.which("openocd")
    if not tool:
        raise Skip("openocd not found")
    # rtt setup/start/channels exist only with a target; the dummy adapter needs no probe.
    text = run([tool, "-c", "adapter driver dummy", "-f", "target/stm32f4x.cfg", "-c", "help rtt",
                "-c", "shutdown"], log=OUT / "openocd-rtt.log")
    for command in ("rtt server start", "rtt setup", "rtt start", "rtt channels"):
        if command not in text:
            raise RuntimeError("OpenOCD has no '%s'" % command)
    script = ROOT / "stm32-rtt-console" / "scripts" / "check_rtt_support.sh"
    report = run(["sh", script], log=OUT / "check_rtt_support.log", cwd=script.parent)
    if "rtt setup <address> <size> <ID>" not in report:
        raise RuntimeError("check_rtt_support.sh does not show 'rtt setup'")
    version = re.search(r"Open On-Chip Debugger (\S+)", text)
    return "RTT commands present, check_rtt_support.sh ok (OpenOCD %s)" % (
        version.group(1) if version else "?")


KITS = [
    ("kits: python scripts compile", check_python),
    ("kits: shell scripts parse", check_shell),
    ("kits: JSON/JSONC files load", check_json),
    ("kits: TOML files load", check_toml),
    ("kits: .clang-format assets", check_clang_format),
    ("embedded-tech-spec: template and example", check_tech_spec),
    ("stm32-renode-emulator: renode_compat", check_renode_compat),
    ("weact-logic-analyzer: self-test", check_weact),
    ("stm32-rtt-console: OpenOCD RTT commands", check_openocd_rtt),
]


# --------------------------------------------------------------------- emulation

class Emulation(object):
    def __init__(self, tools, probe=None):
        self.tools = tools
        self.gcc = tools / "gcc" / "bin"
        self.renode = tools / "renode" / "renode"
        self.svd_g474 = tools / "svd-stm32g474" / "STM32G474.svd"
        self.svd_f411 = tools / "svd-stm32f411" / "STM32F411.svd"
        self.probe = pathlib.Path(probe).resolve() if probe else tools / "probe"
        self.local_probe = probe is not None
        self.elves = {}
        self.env = dict(os.environ, PATH=str(self.gcc) + os.pathsep + os.environ.get("PATH", ""),
                        STM32_TOOLCHAIN_PATH=str(tools / "gcc"))

    def fetch(self):
        names = ["gcc", "renode", "svd-stm32g474", "svd-stm32f411"] + ([] if self.local_probe else ["probe"])
        text = run([sys.executable, "-B", ROOT / "ci" / "fetch_tools.py", "--dest", self.tools] + names,
                   log=OUT / "fetch_tools.log", timeout=900)
        fetched = "; ".join(line.split(None, 1)[0] for line in text.splitlines() if line.strip())
        return fetched + ("; probe from %s" % self.probe if self.local_probe else "")

    def build(self, board):
        """Probe build with semihosting and a timed exit (what run.py of the skill needs)."""
        out = OUT / ("probe-%s" % board.lower())
        if out.exists():
            shutil.rmtree(out)
        args = ["cmake", "-S", self.probe, "-B", out, "-G", "Ninja", "-DBOARD=%s" % board,
                "-DCMAKE_BUILD_TYPE=Debug", "-DPROBE_LOG_SEMIHOSTING=ON", "-DPROBE_EXIT_MS=%d" % PROBE_EXIT_MS,
                "-DPROBE_RENODE_TEST=OFF"]
        if board == "F411CE":
            # Cross-check: the probe builds with the toolchain file of stm32-vscode-cmake-scaffold.
            toolchain = ROOT / "stm32-vscode-cmake-scaffold" / "assets" / "gcc-arm-none-eabi.cmake"
            args += ["-DCMAKE_TOOLCHAIN_FILE=%s" % toolchain, "-DSTM32_TOOLCHAIN_PATH=%s" % (self.tools / "gcc")]
        else:
            args += ["-DARM_TOOLCHAIN_ROOT=%s" % (self.tools / "gcc")]
        run(args, log=OUT / ("probe-%s-configure.log" % board.lower()), env=self.env)
        text = run(["cmake", "--build", out], log=OUT / ("probe-%s-build.log" % board.lower()), env=self.env)
        self.elves[board] = out / "stm32_skills_probe.elf"
        size = re.search(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+\d+", text, re.M)
        toolchain = "scaffold toolchain file" if board == "F411CE" else "probe toolchain"
        return "%s; text %s, data %s, bss %s bytes" % ((toolchain,) + size.groups()) if size else toolchain

    def platform(self, board):
        """Probe model with a semihosting UART added, for the Renode route of run.py."""
        path = OUT / ("probe-%s-semihosting.repl" % board.lower())
        model = (self.probe / "emu" / "renode" / PROBE_MODELS[board]).read_text(encoding="utf-8")
        path.write_text(model.rstrip("\n") + "\n" + SEMIHOSTING_UART, encoding="utf-8")
        return path

    def expected(self, board, emulator):
        source = self.probe / "emu" / "expected" / ("%s.toml" % board.lower())
        if emulator == "renode":
            return source
        path = OUT / ("expected-%s-%s.toml" % (board.lower(), emulator))
        text = source.read_text(encoding="utf-8").replace('emulator = "renode"', 'emulator = "%s"' % emulator)
        path.write_text(text, encoding="utf-8")
        return path

    def emulator_run(self, board, profile, binary_args, emulator):
        output = OUT / ("run-%s-%s" % (board.lower(), emulator))
        if output.exists():
            shutil.rmtree(output)
        run([sys.executable, "-B", RENODE_SKILL / "scripts" / "run.py", "--profile", profile,
             "--elf", self.elves[board], "--output", output, "--verify", "--expected", self.expected(board, emulator),
             "--objcopy", self.gcc / "arm-none-eabi-objcopy"] + binary_args,
            log=OUT / ("run-%s-%s.log" % (board.lower(), emulator)), env=self.env, timeout=240)
        result = json.loads((output / "run.json").read_text(encoding="utf-8"))
        if result.get("status") != "PASS":
            raise RuntimeError("run.json status %s" % result.get("status"))
        log = (output / "startup.log").read_text(encoding="utf-8", errors="replace")
        if "exit: %d ms" % PROBE_EXIT_MS not in log:
            raise RuntimeError("the probe did not reach its timed exit")
        return result

    def renode_boot(self, board):
        result = self.emulator_run(board, "G474", ["--renode", self.renode, "--platform", self.platform(board)], "renode")
        return "boot log PASS in Renode %s (probe model)" % result.get("renode_version")

    def negative_log(self):
        """The checker must reject a truncated log (otherwise PASS above proves nothing)."""
        log = OUT / "run-f411ce-renode" / "startup.log"
        lines = log.read_text(encoding="utf-8").splitlines()
        broken = OUT / "negative" / "startup.log"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", str(RENODE_SKILL / "scripts" / "check_startup_log.py"), "--log", str(broken),
             "--elf", str(self.elves["F411CE"]), "--expected", str(self.expected("F411CE", "renode")),
             "--objcopy", str(self.gcc / "arm-none-eabi-objcopy")], capture_output=True, text=True)
        if result.returncode != 1:
            raise RuntimeError("truncated log accepted (exit %d)" % result.returncode)
        return "truncated log rejected"

    def qemu_boot(self, board):
        qemu = shutil.which("qemu-system-arm")
        if not qemu:
            raise Skip("qemu-system-arm not found")
        self.emulator_run(board, "F411", ["--qemu", qemu], "qemu")
        return "boot log PASS in QEMU netduinoplus2, timed exit"

    def model_vs_svd(self, model_text, svd_path, mapping):
        sys.path.insert(0, str(ROS_SKILL / "scripts"))
        from svd_lookup import SvdLookup
        svd = SvdLookup(svd_path)
        found = dict(re.findall(r"^(\w+): [\w.]+ @ sysbus <?(0x[0-9A-Fa-f]+)", model_text, re.M))
        wrong = []
        for name, peripheral in mapping.items():
            if name not in found:
                wrong.append("%s: not in model" % name)
                continue
            base = svd.peripherals.get(peripheral, {}).get("base")
            if base != int(found[name], 16):
                wrong.append("%s=%s, SVD %s=%s" % (name, found[name], peripheral,
                                                   hex(base) if base is not None else "missing"))
        if wrong:
            raise RuntimeError("; ".join(wrong))
        return "%d addresses match %s" % (len(mapping), svd_path.name)

    def mock_vs_svd(self):
        mock = (RENODE_SKILL / "assets" / "stm32g4_mock.repl").read_text(encoding="utf-8")
        return self.model_vs_svd(mock, self.svd_g474, MOCK_TO_SVD)

    def probe_model_vs_svd(self):
        model = (self.probe / "emu" / "renode" / "stm32f4_probe.repl").read_text(encoding="utf-8")
        return self.model_vs_svd(model, self.svd_f411, PROBE_MODEL_TO_SVD)

    def gdb(self):
        names = ("arm-none-eabi-gdb-py3", "gdb-multiarch", "arm-none-eabi-gdb")
        for name in names:
            path = shutil.which(name, path=self.env["PATH"])
            if path and subprocess.run([path, "-q", "-batch", "-ex", "python print(42)"],
                                       capture_output=True, text=True).stdout.strip() == "42":
                return path
        raise Skip("no GDB with Python (%s)" % ", ".join(names))

    def resolve(self, board):
        """Probe fields through resolve_addresses.py: symbols, a struct field by DWARF, SVD registers (F411)."""
        fields = [("/probe/counter", "&probe_counter"), ("/probe/state", "&probe_state"),
                  ("/probe/frames", "(char*)&probe_telemetry + (long)&((ProbeTelemetry*)0)->frames"),
                  ("/probe/work_cycles", "(char*)&probe_telemetry + (long)&((ProbeTelemetry*)0)->work_cycles")]
        extra = []
        if board == "F411CE":
            fields += [("/rcc/cr_hseon", "svd:RCC.CR.HSEON"), ("/gpiob/odr", "svd:GPIOB.ODR")]
            extra = ["--svd", self.svd_f411]
        spec = OUT / ("fields-%s.toml" % board.lower())
        spec.write_text("".join('[[field]]\ntopic = "%s"\nmessage = "std_msgs/UInt32"\nkind = "uint32"\nexpr = "%s"\n\n'
                                % (topic, expr) for topic, expr in fields), encoding="utf-8")
        out = OUT / ("addresses-%s.json" % board.lower())
        run([sys.executable, "-B", ROS_SKILL / "scripts" / "resolve_addresses.py", "--elf", self.elves[board],
             "--spec", spec, "--out", out] + extra,
            log=OUT / ("resolve-%s.log" % board.lower()), env=dict(self.env, GDB=self.gdb()), timeout=120)
        resolved = {item["expr"]: item["address"] for item in json.loads(out.read_text(encoding="utf-8"))["fields"]}
        symbols = {}
        for line in run([self.gcc / "arm-none-eabi-nm", self.elves[board]]).splitlines():
            parts = line.split()
            if len(parts) == 3:
                symbols[parts[2]] = int(parts[0], 16)
        for symbol in ("probe_counter", "probe_state"):
            if resolved["&" + symbol] != symbols[symbol]:
                raise RuntimeError("%s: %#x, nm %#x" % (symbol, resolved["&" + symbol], symbols[symbol]))
        # ProbeTelemetry: frames at offset 0, work_cycles at 20 (src/probe.h).
        base = symbols["probe_telemetry"]
        if resolved[fields[2][1]] != base or resolved[fields[3][1]] != base + 20:
            raise RuntimeError("struct fields: %s, probe_telemetry %#x" % (resolved, base))
        if board == "F411CE" and (resolved["svd:RCC.CR.HSEON"] != 0x40023800 or resolved["svd:GPIOB.ODR"] != 0x40020414):
            raise RuntimeError("SVD fields: %s" % resolved)
        return resolved, fields

    def resolve_offline(self):
        self.resolve("F411CE")
        return "symbols match nm, struct fields by DWARF, F411 SVD registers"

    def live(self, board):
        """Read the resolved RAM addresses twice from the running core in Renode (route A idea)."""
        resolved, fields = self.resolve(board)
        counter, state, frames = resolved["&probe_counter"], resolved["&probe_state"], resolved[fields[2][1]]
        work = OUT / ("live-%s" % board.lower())
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        script = work / "live.resc"
        script.write_text("\n".join([
            "logLevel 3", "using sysbus", 'mach create "probe"',
            "machine LoadPlatformDescription @%s" % self.platform(board).as_posix(),
            "cpu.semihosting.uart CreateFileBackend @%s true" % (work / "startup.log").as_posix(),
            "sysbus LoadELF @%s" % self.elves[board].as_posix(),
            'emulation RunFor "0.15"', 'echo "A="', "sysbus ReadDoubleWord %#x" % counter,
            'echo "F="', "sysbus ReadDoubleWord %#x" % frames,
            'emulation RunFor "0.15"', 'echo "B="', "sysbus ReadDoubleWord %#x" % counter,
            'echo "G="', "sysbus ReadDoubleWord %#x" % frames,
            'echo "S="', "sysbus ReadDoubleWord %#x" % state, "quit", ""]), encoding="utf-8")
        text = run([self.renode, "--disable-gui", "--console", "--plain", "-e", "include @%s" % script.as_posix()],
                   log=work / "renode.log", timeout=120)
        values = {key: int(value, 16) for key, value in re.findall(r"^([ABFGS])=\s*\n(0x[0-9A-Fa-f]+)", text, re.M)}
        if set(values) != set("ABFGS"):
            raise RuntimeError("unexpected monitor output: %s" % text.strip()[-300:])
        if not 0 < values["A"] < values["B"] or not values["F"] < values["G"] or values["S"] != 0xA5:
            raise RuntimeError("counter %(A)d -> %(B)d, frames %(F)d -> %(G)d, state %(S)#x" % values)
        return "probe_counter %(A)d -> %(B)d, frames %(F)d -> %(G)d, probe_state %(S)#x" % values


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stages", nargs="+", choices=["kits", "emulation"])
    parser.add_argument("--strict", action="store_true", help="SKIP counts as failure")
    parser.add_argument("--tools", type=pathlib.Path, default=ROOT / "build" / "ci-tools",
                        help="directory for ci/fetch_tools.py downloads")
    parser.add_argument("--probe", type=pathlib.Path,
                        help="local stm32-skills-probe checkout instead of the pinned commit (development)")
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    runner = Runner(args.strict)
    if "kits" in args.stages:
        for name, function in KITS:
            runner.check(name, function)
    if "emulation" in args.stages:
        emulation = Emulation(args.tools.resolve(), args.probe)
        steps = [("emulation: fetch pinned tools", emulation.fetch)]
        for board in BOARDS:
            steps.append(("stm32-skills-probe: build %s" % board, lambda b=board: emulation.build(b)))
        for board in BOARDS:
            steps.append(("stm32-renode-emulator: Renode %s" % board, lambda b=board: emulation.renode_boot(b)))
        steps.append(("stm32-renode-emulator: truncated log fails", emulation.negative_log))
        for board in ("F411CE", "F401CC"):
            steps.append(("stm32-renode-emulator: QEMU %s" % board, lambda b=board: emulation.qemu_boot(b)))
        steps += [
            ("renode mock vs G474 SVD (svd_lookup)", emulation.mock_vs_svd),
            ("probe model vs F411 SVD (svd_lookup)", emulation.probe_model_vs_svd),
            ("stm32-ros-alt-channel: resolve offline", emulation.resolve_offline),
            ("stm32-ros-alt-channel: read RAM in Renode F411CE", lambda: emulation.live("F411CE")),
            ("stm32-ros-alt-channel: read RAM in Renode F103C8", lambda: emulation.live("F103C8")),
        ]
        for name, function in steps:
            runner.check(name, function)
            if name.startswith(("emulation: fetch", "stm32-skills-probe")) and runner.results[-1]["status"] != "PASS":
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
