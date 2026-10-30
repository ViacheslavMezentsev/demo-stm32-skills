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
           stm32-diag-record records the probe fields through a fake OpenOCD telnet
           (ci/fake_openocd.py) into mcap and bag files, read back by the mcap and rosbags
           Python packages.

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
GDBPY_SKILL = ROOT / "stm32-gdbpy-scenario"

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


def check_weact_integrity():
    script = ROOT / "weact-logic-analyzer" / "scripts" / "test_capture_integrity.py"
    run([sys.executable, "-B", script], log=OUT / "weact-integrity.log", timeout=30)
    return "complete, short rc=0, excess, wrong rate, corrupt, missing, exit error, invalid samples"


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


def check_diag_helpers():
    sys.path.insert(0, str(ROOT / "stm32-diag-record" / "scripts"))
    import record_diag as diag
    words = {0x100: 0x44332211, 0x104: 0x88776655}
    assert diag.word_at(words, 0x100) == 0x44332211 and diag.word_at(words, 0x102) == 0x66554433, "word_at"
    assert diag.word_at(words, 0x106) is None, "word_at beyond the read words"
    assert diag.convert(0xFFFFFFFF, "int32") == -1 and diag.convert(0x8000, "uint32", 15, 0x8000) == 1, "convert"
    assert diag.convert(2500, "uint32_seconds") == 2 and diag.convert(0x3F800000, "float32") == 1.0, "convert"
    assert diag.aligned_needed(0x103) == [0x100, 0x104], "unaligned field reads two words"
    return "unaligned words, kinds, shift and mask"


# OpenOCD 0.12 output shapes for the doctor parser (adapters and targets other than ST-Link and STM32 too).
OPENOCD_STLINK_OK = """Open On-Chip Debugger 0.12.0
Info : clock speed 2000 kHz
Info : STLINK V2J37S7 (API v2) VID:PID 0483:3748
Info : Target voltage: 3.235294
Info : [stm32f4x.cpu] Cortex-M4 r0p1 processor detected
Info : [stm32f4x.cpu] target has 6 breakpoints, 4 watchpoints
    TargetName         Type       Endian TapName            State
--  ------------------ ---------- ------ ------------------ ------------
 0* stm32f4x.cpu       hla_target little stm32f4x.cpu       running
0x20000010: 0001e240
"""
OPENOCD_CMSIS_DAP_RP2040 = """Info : Using CMSIS-DAPv2 interface with VID:PID=0x2e8a:0x000c, serial=0123456789AB
Info : CMSIS-DAP: SWD supported
Info : CMSIS-DAP: FW Version = 2.0.0
Info : [rp2040.core0] Cortex-M0+ r0p1 processor detected
Info : [rp2040.core0] Examination succeed
    TargetName         Type       Endian TapName            State
--  ------------------ ---------- ------ ------------------ ------------
 0* rp2040.core0       cortex_m   little rp2040.cpu         running
 1  rp2040.core1       cortex_m   little rp2040.cpu         running
"""
OPENOCD_NO_JLINK = "Error: No J-Link device found\n"
OPENOCD_NO_CONFIG = "embedded:startup.tcl:28: Error: Can't find target/at32f403axx.cfg\nin procedure 'script'\n"


def check_diag_doctor():
    sys.path.insert(0, str(ROOT / "stm32-diag-record" / "scripts"))
    import record_diag as diag
    ok = diag.parse_openocd(OPENOCD_STLINK_OK)
    assert ok["examined"] and ok["core"] == "Cortex-M4" and ok["word"] == (0x20000010, 0x1E240), ok
    assert ok["targets"] == [{"name": "stm32f4x.cpu", "type": "hla_target", "state": "running"}], ok["targets"]
    assert any("STLINK" in line for line in ok["adapter"]), ok["adapter"]
    dap = diag.parse_openocd(OPENOCD_CMSIS_DAP_RP2040)
    assert dap["examined"] and dap["core"] == "Cortex-M0+" and len(dap["targets"]) == 2, dap
    assert any("CMSIS-DAP" in line for line in dap["adapter"]) and dap["word"] is None, dap
    jlink = diag.parse_openocd(OPENOCD_NO_JLINK)
    assert not jlink["examined"] and jlink["errors"] and not jlink["missing"], jlink
    assert diag.parse_openocd(OPENOCD_NO_CONFIG)["missing"] == ["target/at32f403axx.cfg"], "missing config"

    class Args(object):
        openocd, interface, serial, target, port, reset = "openocd", "interface/jlink.cfg", "", "target/x.cfg", 4444, False
        openocd_command = ["adapter speed 4000", "reset_config none"]
    command = diag.openocd_probe_command(Args, 0x20000013)
    joined = " ".join(command)
    assert joined.index("reset_config none") < joined.index(" init") and "mdw 0x20000010" in joined, joined
    assert "telnet_port disabled" in joined and diag.openocd_command(Args)[-1] == "init", joined
    return "ST-Link, CMSIS-DAP (two cores), J-Link absent, missing config; extra commands before init"


def check_diag_telnet_async():
    """Асинхронные сообщения OpenOCD ('error during read: Bad file descriptor' о закрытом чужом
    соединении, Windows) не сдвигают ответы telnet: ответ отсчитывается от эха команды."""
    sys.path.insert(0, str(ROOT / "ci"))
    sys.path.insert(0, str(ROOT / "stm32-diag-record" / "scripts"))
    sys.path.insert(0, str(ROOT / "stm32-diag-record" / "scripts" / "rtt"))
    import socket as _socket
    from fake_openocd import FakeOpenOcd
    import record_diag as diag
    import rtt_timer
    vtor, rcc = 0xE000ED08, 0x40023840
    memory = {vtor: lambda n: 0x08000000, rcc: lambda n: 0x00000011, 0x20000000: lambda n: 0x100 + n}
    # Помеха настоящая: наивное чтение до первого приглашения возвращает не ответ на mdw.
    with FakeOpenOcd(memory, noise={0: "before"}) as server:
        raw = _socket.create_connection(("127.0.0.1", server.port), timeout=2)
        data = b""
        while b"> " not in data:
            data += raw.recv(4096)
        raw.sendall(b"mdw 0xE000ED08\n")
        data = b""
        while b"\n> " not in data:
            data += raw.recv(4096)
        raw.close()
        assert b"0xe000ed08:" not in data.split(b"\n> ")[0], "noise did not precede the answer"
    noise = {0: "before", 1: "midecho", 3: "before", 4: "midecho"}
    with FakeOpenOcd(memory, noise=noise) as server:
        telnet = diag.Telnet("127.0.0.1", server.port)
        assert rtt_timer.mdw(telnet, vtor) == 0x08000000, "VTOR after async message"
        assert rtt_timer.mdw(telnet, rcc) == 0x11, "RCC after mid-echo message"
        telnet.command("halt")
        words = diag.read_words(telnet, [0x20000000])
        assert words == {0x20000000: 0x100}, words
        assert rtt_timer.mdw(telnet, rcc) == 0x11 and rtt_timer.mdw(telnet, vtor) == 0x08000000, "responses shifted"
        telnet.close()
    return "async OpenOCD messages before and inside the echo: mdw/read_words stay aligned"


def check_site():
    """site/ (GitHub Pages): local links and assets exist, no external scripts or styles, theme button on every page."""
    site = ROOT / "site"
    pages = sorted(site.rglob("*.html"))
    if not pages:
        raise RuntimeError("no pages in site/")
    problems = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        name = page.relative_to(site).as_posix()
        for needle in ('<html lang="ru"', "<title>", 'id="theme-toggle"', "assets/theme.js", "assets/site.css"):
            if needle not in text:
                problems.append("%s: no %s" % (name, needle))
        if re.search(r"<(?:script|link)[^>]+(?:src|href)=\"(?:https?:)?//", text):
            problems.append("%s: external script or stylesheet" % name)
        for target in re.findall(r"(?:href|src)=\"([^\"#]*)", text):
            if not target or re.match(r"(?:https?:|mailto:)", target):
                continue
            path = (page.parent / target).resolve()
            if path.is_dir():
                path = path / "index.html"
            if not path.is_file() or site.resolve() not in path.parents:
                problems.append("%s: broken link %s" % (name, target))
    if problems:
        raise RuntimeError("; ".join(problems))
    return "%d pages, local links and assets resolve" % len(pages)


def validate_renode_record(values):
    """Functional recording check; wall-clock polling throughput is not a CI contract."""
    required = {"/probe/tick_ms", "/probe/loop_counter", "/probe/pin_frame"}
    if len(values) != 8 or not required <= values.keys():
        raise RuntimeError("Renode recording topics: expected 8 including %s, got %s" %
                           (sorted(required), sorted(values)))
    counts = {topic: len(samples) for topic, samples in values.items()}
    if len(set(counts.values())) != 1 or min(counts.values()) < 2:
        raise RuntimeError("Renode recording needs >=2 complete cycles; counts=%s" % counts)
    for topic in ("/probe/tick_ms", "/probe/loop_counter"):
        samples = values[topic]
        if any(b < a for a, b in zip(samples, samples[1:])) or samples[-1] <= samples[0]:
            raise RuntimeError("Renode counter must advance without going backwards: %s, count=%d, values=%s" %
                               (topic, len(samples), samples))
    pins = set(values["/probe/pin_frame"])
    if pins - {0, 1}:
        raise RuntimeError("Renode pin_frame outside {0, 1}: %s" % sorted(pins))


def check_renode_record_validation():
    run([sys.executable, "-B", ROOT / "ci/test_diag_renode_samples.py"], log=OUT / "diag-renode-samples.log")
    return "slow polling accepted; incomplete, stalled, backwards and invalid samples rejected"


def check_rtt_cleanup():
    run([sys.executable, "-B", ROOT / "ci/test_rtt_cleanup.py"], log=OUT / "rtt-cleanup.log")
    return "active ISR drain, stuck ISR, readback failure, RCC ownership"


def check_rtt_record(readers=False):
    args = [sys.executable, "-B", ROOT / "ci/test_rtt_record.py"]
    if readers:
        args.append("--readers")
    run(args, log=OUT / ("rtt-record-readers.log" if readers else "rtt-record.log"))
    return "raw seq/tick, wrap, gaps, filtered fields, EOF, Ctrl+C and transport failure"


def check_rtt_frames():
    """Парсер кадров rtt-timer: ресинхронизация, склейка кусков, разрывы seq, kind/shift/mask."""
    sys.path.insert(0, str(ROOT / "stm32-diag-record" / "scripts" / "rtt"))
    import struct as _struct
    import rtt_frames as rf
    layout = {"frame": {"len": 8 + 4 * 3, "header": 8, "magic": 0xD1A6, "count": 3},
              "fields": [{"topic": "/a", "message": "std_msgs/UInt32", "kind": "uint32", "byte_offset": 0},
                         {"topic": "/b", "message": "std_msgs/Float32", "kind": "float32", "byte_offset": 0},
                         {"topic": "/pin", "message": "std_msgs/UInt32", "kind": "uint32",
                          "byte_offset": 0, "shift": 15, "mask": 0x8000}]}
    reader = rf.FrameReader(layout)
    as_bits = lambda value: _struct.unpack("<I", _struct.pack("<f", value))[0]
    stream = (b"\x11\x22" + rf.build_frame(1, 1000, [10, as_bits(0.5), 0x8000])
              + rf.build_frame(2, 1020, [11, as_bits(1.5), 0x0000])
              + rf.build_frame(5, 1080, [14, as_bits(2.5), 0x8000]))   # пропущены seq 3, 4
    frames = []
    for i in range(0, len(stream), 7):      # куски по 7 байт: проверяем склейку границ
        frames += reader.feed(stream[i:i + 7])
    assert [f[0] for f in frames] == [1, 2, 5], "seq %s" % [f[0] for f in frames]
    assert frames[0][2][0][2] == 10 and abs(frames[0][2][1][2] - 0.5) < 1e-6, "uint/float"
    assert frames[0][2][2][2] == 1 and frames[1][2][2][2] == 0, "shift/mask"
    assert reader.stats["seq_gaps"] == 1 and reader.stats["dropped_est"] == 2 and reader.stats["resyncs"] >= 1, reader.stats
    import rtt_timer
    assert rtt_timer.apb1_timer_clock(25_000_000, 0) == (25_000_000, 1), "APB1 /1"
    assert rtt_timer.apb1_timer_clock(100_000_000, 4) == (100_000_000, 2), "APB1 /2 -> x2"
    assert rtt_timer.apb1_timer_clock(100_000_000, 5) == (50_000_000, 4), "APB1 /4"
    return "resync, chunking, seq-gap drops (2), kinds/shift/mask; APB1 timer-clock decode"


def check_debug_tools():
    run([sys.executable, "-B", ROOT / "ci/test_ros_clock_validation.py"], log=OUT / "ros-clock-validation.log")
    for skill in ("stm32-ros-alt-channel", "stm32-diag-record"):
        run([sys.executable, "-B", ROOT / "ci/test_shared_debug_tools.py"],
            env=dict(os.environ, SHARED_TEST_SKILL=skill), log=OUT / (skill + "-shared.log"))
    return "both skill copies: TCP noise, ROS publishing, masks, GDB selection, process ownership"


def check_gdbpy_ocd():
    """ocd.py: разбор действий, команды одноразового сеанса и ответы OpenOCD без пробника."""
    sys.path.insert(0, str(GDBPY_SKILL / "scripts"))
    import ocd
    actions = ocd.parse_actions(["state", "read", "0x20000000", "2", "resume", "raw", "reg pc"])
    sent = ocd.commands(actions, True)
    if "halt" not in sent[sent.index("echo {@@2}"):] or "mdw 0x20000000 2" not in sent:
        raise RuntimeError("one-shot commands: %s" % sent)
    if "halt" in ocd.commands(ocd.parse_actions(["resume"]), False):
        raise RuntimeError("server resume must not halt")
    text = ("Info : noise\n@@0\n0xe000edf0: 00030003 \n@@1\n0x20000000: 000000a5 00000010 \n"
            "@@2\nError: not halted\n@@3\npc (/32): 0x08000131\n@@end\n")
    import contextlib
    import io
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        ocd.report(actions, text)
    lines = stream.getvalue().splitlines()
    expected = ["state HALTED (DHCSR 0x00030003)", "0x20000000 0x000000A5", "0x20000004 0x00000010", "resume ERROR"]
    if lines[:4] != expected or "pc (/32): 0x08000131" not in lines:
        raise RuntimeError("report: %s" % lines)
    states = [ocd.decode_state(v) for v in (0x01010001, 0x00050001, 0x00030003, 0x00080001)]
    if states != ["RUNNING", "SLEEPING", "HALTED", "LOCKUP"]:
        raise RuntimeError("DHCSR decode: %s" % states)
    if ocd.remote_path("~/.local/x y/openocd") != '"$HOME"/\'.local/x y/openocd\'':
        raise RuntimeError("remote path quoting")
    return "actions, one-shot/server resume, report and DHCSR decode"


def check_gdbpy_environment():
    """environment.py и run_scenario.py: опознание отладчиков и журналов OpenOCD, пути на узле, сводка."""
    sys.path.insert(0, str(GDBPY_SKILL / "scripts"))
    import environment
    import run_scenario
    if environment.classify("0483", "3748", "STM32 STLink") != ("stlink", "ST-Link V2") or \
            environment.classify("1366", "0105", "J-Link")[0] != "jlink" or \
            environment.classify("abcd", "0001", "Board CMSIS-DAP")[0] != "cmsis-dap":
        raise RuntimeError("classify")
    if environment.serial_text("303636394646") != "0669FF" or environment.serial_text("ff0a1b") is not None:
        raise RuntimeError("serial_text")
    old = ("Info : STLINK V2J17S4 (API v2) VID:PID 0483:3748\nInfo : Target voltage: 3.241\n"
           "Info : stm32f4x.cpu: Cortex-M4 r0p1 processor detected\n0xe000ed00: 410fc241 \n0xe0042000: 10016419 \n")
    info = environment.parse_identify(old)
    if (info.get("firmware"), info.get("stlink_jtag"), info.get("voltage"), info.get("core"), info.get("dev_id"),
            info.get("target")) != ("V2J17S4", 17, 3.241, "Cortex-M4", "0x419", "stm32f4x"):
        raise RuntimeError("ST-Link log: %s" % info)
    m0 = environment.parse_identify("0xe000ed00: 410cc200 \n0x40015800: 20006440 \n")
    if (m0.get("core"), m0.get("dev_id")) != ("Cortex-M0", "0x440"):
        raise RuntimeError("Cortex-M0 IDCODE: %s" % m0)
    jl = environment.parse_identify("Info : Firmware: J-Link V11 compiled Dec  4 2024\nInfo : VTarget = 3.300 V\n"
                                    "Error: Error connecting DP: cannot read IDR\n")
    if (jl.get("firmware"), jl.get("voltage"), jl.get("error")) != (
            "J-Link V11 compiled Dec  4 2024", 3.3, "нет связи с целью (питание, провода SWD, режим сна)"):
        raise RuntimeError("J-Link log: %s" % jl)
    probe = {"place": "remote", "host": "user@stand", "kind": "stlink", "serial": None, "location": "7-1.1.3",
             "firmware": "V2J17S4", "dev_id": "0x419", "target": "stm32f4x"}
    if environment.fingerprint(probe) != "remote:user@stand|stlink|usb:7-1.1.3|V2J17S4|0x419|-":
        raise RuntimeError("fingerprint: %s" % environment.fingerprint(probe))
    args = type("A", (), {"ssh_key": None, "openocd_remote": "~/ocd/bin/openocd"})()
    if environment.runner_args(args, probe)[-2:] != ["--adapter-usb-location", "7-1.1.3"]:
        raise RuntimeError("runner args")
    command = environment.identify_command(dict(probe, serial="ABC"), "openocd")
    if "adapter serial ABC" not in command or "reset" in " ".join(command) or "shutdown" not in command:
        raise RuntimeError("identify command must read only: %s" % command)
    for path in ("~/x/openocd", "$HOME/x/openocd", "${HOME}/x/openocd"):
        if run_scenario.remote_path(path) != '"$HOME"/x/openocd' or environment.remote_path(path) != '"$HOME"/x/openocd':
            raise RuntimeError("remote path %s" % path)
    line = run_scenario.summary_line({"status": "FAIL", "checks": {"a": True, "b": False}, "elapsed_s": 8.6})
    if line != "FAIL: проверок 1/2, 8.6 с; не прошли: b":
        raise RuntimeError("summary: %s" % line)
    uid = environment.parse_identify("0xe000ed00: 410fc241 \n0xe0042000: 10006431 \n"
                                     "0x1fff7a10: 00410037 3235510d 37383836 \n")
    if not re.fullmatch(r"uid#[0-9a-f]{8}", uid.get("uid", "")) or "37383836" in json.dumps(uid):
        raise RuntimeError("UID must be hashed: %s" % uid)
    if not environment.fingerprint(dict(probe, uid=uid["uid"])).endswith("|0x419|" + uid["uid"]):
        raise RuntimeError("fingerprint with UID")
    if "array set u" not in " ".join(environment.identify_command(probe, "openocd")):
        raise RuntimeError("identify command must read UID")
    return "probe types, old ST-Link firmware, Cortex-M0 IDCODE, J-Link errors, fingerprint, UID hash, paths, summary"


def tiny_elf(text, data, build_id=None):
    """ELF32 Cortex-M из трёх секций: .text во Flash, .data с LMA во Flash и VMA в ОЗУ, .bss."""
    import struct
    names = b"\0.text\0.data\0.bss\0.note.gnu.build-id\0.shstrtab\0"
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\0" + build_id if build_id else b""
    body_off = 52 + 32 * 2
    text_off, note_off = body_off, body_off + len(text)
    data_off = note_off + len(note)
    names_off = data_off + len(data)
    sh_off = (names_off + len(names) + 3) & ~3
    flash, ram = 0x08000000, 0x20000000
    phdrs = struct.pack("<8I", 1, text_off, flash, flash, len(text) + len(note), len(text) + len(note), 5, 4)
    phdrs += struct.pack("<8I", 1, data_off, ram, flash + len(text) + len(note), len(data), len(data) + 16, 6, 4)
    sections = [struct.pack("<10I", *([0] * 10)),
                struct.pack("<10I", 1, 1, 6, flash, text_off, len(text), 0, 0, 4, 0),
                struct.pack("<10I", 7, 1, 3, ram, data_off, len(data), 0, 0, 4, 0),
                struct.pack("<10I", 13, 8, 3, ram + len(data), data_off + len(data), 16, 0, 0, 4, 0),
                struct.pack("<10I", 18, 7, 2, flash + len(text), note_off, len(note), 0, 0, 4, 0),
                struct.pack("<10I", 37, 3, 0, 0, names_off, len(names), 0, 0, 1, 0)]
    header = b"\x7fELF\x01\x01\x01" + b"\0" * 9 + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, flash, 52, sh_off, 0x05000200,
                                                                52, 32, 2, 40, len(sections), 5)
    blob = header + phdrs + text + note + data + names
    return blob + b"\0" * (sh_off - len(blob)) + b"".join(sections)


def check_gdbpy_style_image():
    """check_scenario.py по правилам SC и сверка образа elfimage.py на синтетическом ELF, без GDB и платы."""
    sys.path.insert(0, str(GDBPY_SKILL / "scripts"))
    import check_scenario
    import elfimage
    import run_scenario
    for asset in sorted((GDBPY_SKILL / "assets").glob("*.py")):
        result = check_scenario.check(asset)
        if result["errors"] or result["warnings"]:
            raise RuntimeError("%s: %s" % (asset.name, result["findings"]))
    template = (GDBPY_SKILL / "assets" / "scenario_template.py").read_text(encoding="utf-8")
    section = re.compile(r"(    # -{10,} Сценарий\n).*?(    # -{20,}\n)", re.S)
    bad = ('    raw = read("*(uint32_t *)0x20000104")\n'
           '    field = read("*(uint16_t *)((char *)&g_app + 12)")\n'
           '    base = int(gdb.parse_and_eval("&g_app"))\n    word = read_running(base + 8)\n'
           '    item = read_running(base + i * 4)\n'
           '    gpio = read_running(0x40020014)\n    host = "10.1.2.3"\n'
           '    check("value", read("g_app.mode") == 42)\n')
    with tempfile.TemporaryDirectory() as folder:
        path = pathlib.Path(folder) / "bad.py"
        path.write_text(section.sub(lambda m: m.group(1) + bad + m.group(2), template), encoding="utf-8")
        found = check_scenario.check(path)
        rules = sorted({(f["rule"], f["level"]) for f in found["findings"]})
        expect = [("SC01", "error"), ("SC02", "error"), ("SC04", "error"), ("SC05", "error"), ("SC09", "warning")]
        lines = sorted(f["line"] for f in found["findings"] if f["rule"] == "SC04")
        if rules != expect or len(lines) != 2:
            raise RuntimeError("style rules: %s, offsets at %s" % (rules, lines))
        allowed = path.read_text(encoding="utf-8").replace(
            "base + 8)", "base + 8)  # scenario: allow-offset: раскладка зафиксирована в протоколе")
        path.write_text(allowed, encoding="utf-8")
        if len([f for f in check_scenario.check(path)["findings"] if f["rule"] == "SC04" and f["level"] == "error"]) != 1:
            raise RuntimeError("allow-offset marker ignored")
        elf_path = pathlib.Path(folder) / "app.elf"
        elf_path.write_bytes(tiny_elf(b"\x11" * 64, b"\x22" * 8, build_id=b"\xab" * 20))
        elf = elfimage.Elf(elf_path)
        layout = [(name, hex(address), len(body)) for name, address, body in elf.flash_sections()]
        if layout != [(".text", "0x8000000", 64), (".note.gnu.build-id", "0x8000040", 36), (".data", "0x8000064", 8)]:
            raise RuntimeError("flash sections: %s" % layout)
        if elf.build_id() != (0x08000050, b"\xab" * 20):
            raise RuntimeError("build-id: %s" % (elf.build_id(),))
        flash = bytearray(0x200)
        for _, address, body in elf.flash_sections():
            flash[address - 0x08000000:address - 0x08000000 + len(body)] = body
        def read(address, length):
            return bytes(flash[address - 0x08000000:address - 0x08000000 + length])
        if elfimage.compare(read, elf.flash_sections())["status"] != "match":
            raise RuntimeError("compare: own image")
        flash[0x66] ^= 0xFF
        diff = elfimage.compare(read, elf.flash_sections(), chunk=4)
        if diff["status"] != "mismatch" or diff["first_diff"]["address"] != "0x08000066" or \
                diff["first_diff"]["section"] != ".data":
            raise RuntimeError("compare: mismatch %s" % diff)
    args = type("A", (), {"image_check": "auto", "elf": "app.elf", "renode_platform": None, "environment": None})()
    modes = [run_scenario.image_mode(args)]
    args.renode_platform = "board.repl"
    modes.append(run_scenario.image_mode(args))
    args.image_check = "full"
    modes.append(run_scenario.image_mode(args))
    args.renode_platform, args.image_check = None, "build-id"
    modes.append(run_scenario.image_mode(args))
    if modes != ["full", "not-needed", "not-needed", "build-id"]:
        raise RuntimeError("image modes: %s" % modes)
    line = run_scenario.summary_line({"status": "PASS", "checks": {"a": True}, "elapsed_s": 2.0,
                                      "image": {"status": "match"}})
    if line != "PASS: проверок 1/1, 2.0 с, образ совпадает с ELF":
        raise RuntimeError("summary: %s" % line)
    return "assets clean, SC01/02/04/05/09 and allow-offset, ELF sections with LMA, build-id, mismatch address"


def check_gdbpy_jlink():
    """jlink.py: группы команд JLinkExe и разбор ответа Commander без пробника."""
    sys.path.insert(0, str(GDBPY_SKILL / "scripts"))
    import jlink
    actions = jlink.parse_actions(["state", "read", "0x20000000", "2", "resume", "reset-run", "raw", "regs"])
    sent = jlink.commands(actions)
    if sent != ["sleep 0", "mem32 E000EDF0, 1", "sleep 0", "mem32 20000000, 2", "sleep 0", "g", "sleep 0", "r", "g",
                "sleep 0", "regs", "sleep 0", "q"]:
        raise RuntimeError("commands: %s" % sent)
    text = ("Cortex-M4 identified.\nJ-Link>sleep 0\nJ-Link>mem32 E000EDF0, 1\nE000EDF0 = 01050001 \n"
            "J-Link>sleep 0\nJ-Link>mem32 20000000, 2\n20000000 = 000000A5 00000011 \nJ-Link>sleep 0\nJ-Link>g\n"
            "****** Error: CPU is not halted\nJ-Link>sleep 0\nJ-Link>r\nJ-Link>g\nJ-Link>sleep 0\nJ-Link>regs\n"
            "PC = 080000F4, CycleCnt = 00000000\nJ-Link>sleep 0\nJ-Link>q\n")
    import contextlib
    import io
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        jlink.report(actions, text)
    lines = stream.getvalue().splitlines()
    expected = ["state SLEEPING (DHCSR 0x01050001)", "0x20000000 0x000000A5", "0x20000004 0x00000011",
                "resume ok (already running)", "reset-run ok", "PC = 080000F4, CycleCnt = 00000000"]
    if lines != expected:
        raise RuntimeError("report: %s" % lines)
    return "command groups, mem32 parsing, resume of a running core"


def check_shared():
    text = run([sys.executable, "-B", ROOT / "ci" / "sync_shared.py", "--check"])
    return text.strip().splitlines()[-1]


KITS = [
    ("kits: shared modules match their originals", check_shared),
    ("kits: shared debug tools in both skills", check_debug_tools),
    ("pages: site/ links and assets", check_site),
    ("kits: python scripts compile", check_python),
    ("kits: shell scripts parse", check_shell),
    ("kits: JSON/JSONC files load", check_json),
    ("kits: TOML files load", check_toml),
    ("kits: .clang-format assets", check_clang_format),
    ("embedded-tech-spec: template and example", check_tech_spec),
    ("stm32-gdbpy-scenario: ocd.py parsing", check_gdbpy_ocd),
    ("stm32-gdbpy-scenario: jlink.py parsing", check_gdbpy_jlink),
    ("stm32-gdbpy-scenario: environment and runner helpers", check_gdbpy_environment),
    ("stm32-gdbpy-scenario: scenario style and image check", check_gdbpy_style_image),
    ("stm32-renode-emulator: renode_compat", check_renode_compat),
    ("weact-logic-analyzer: capture integrity", check_weact_integrity),
    ("weact-logic-analyzer: self-test", check_weact),
    ("stm32-rtt-console: OpenOCD RTT commands", check_openocd_rtt),
    ("stm32-diag-record: conversion helpers", check_diag_helpers),
    ("stm32-diag-record: doctor parses OpenOCD output", check_diag_doctor),
    ("stm32-diag-record: telnet survives async OpenOCD messages", check_diag_telnet_async),
    ("stm32-diag-record: rtt-timer frame parser", check_rtt_frames),
    ("stm32-diag-record: RTT recording evidence", check_rtt_record),
    ("stm32-diag-record: GDB cleanup state machine", check_rtt_cleanup),
    ("stm32-diag-record: Renode recording assertions", check_renode_record_validation),
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

    def gdbpy_run(self, name, scenario, timeout=60, extra=()):
        """run_scenario.py навыка stm32-gdbpy-scenario: Renode с GDB-сервером, зонд F411CE."""
        out = OUT / ("gdbpy-%s" % name)
        if out.exists():
            shutil.rmtree(out)
        proc = subprocess.run([sys.executable, "-B", str(GDBPY_SKILL / "scripts" / "run_scenario.py"),
                               "--elf", str(self.elves["F411CE"]), "--scenario", str(scenario),
                               "--renode-platform", str(self.platform("F411CE")), "--renode", str(self.renode),
                               "--gdb", self.gdb(), "--out", str(out), "--timeout", str(timeout)] + list(extra),
                              capture_output=True, text=True, timeout=timeout + 90, env=self.env)
        (OUT / ("gdbpy-%s.log" % name)).write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if not (out / "run.json").is_file():
            raise RuntimeError("no run.json (exit %d): %s" % (proc.returncode, (proc.stdout + proc.stderr)[-300:]))
        return proc.returncode, json.loads((out / "run.json").read_text(encoding="utf-8"))

    def gdbpy_probe(self):
        code, result = self.gdbpy_run("probe", GDBPY_SKILL / "assets" / "probe_scenario.py")
        if code != 0 or result["status"] != "PASS" or result.get("restored") is not True:
            raise RuntimeError("exit %d, status %s, restored %s, checks %s"
                               % (code, result["status"], result.get("restored"), result["checks"]))
        ticks = [item["data"] for item in result["records"]]
        return "%d checks PASS, frame ticks %s, period restored" % (len(result["checks"]), ticks)

    def gdbpy_image_style(self):
        """В эмуляторе сверка образа не выполняется (модель загрузила тот же ELF); отказ запуска при нарушении стиля."""
        code, result = self.gdbpy_run("image", GDBPY_SKILL / "assets" / "probe_scenario.py",
                                      extra=["--image-check", "full"])
        image = result.get("image") or {}
        if code != 0 or image.get("status") != "not-needed" or not result.get("elf_sha256"):
            raise RuntimeError("image check in Renode: exit %d, %s" % (code, image))
        template = (GDBPY_SKILL / "assets" / "scenario_template.py").read_text(encoding="utf-8")
        section = re.compile(r"(    # -{10,} Сценарий\n).*?(    # -{20,}\n)", re.S)
        path = OUT / "gdbpy-variants" / "style.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(section.sub(lambda m: m.group(1) + '    raw = read("*(uint32_t *)0x20000104")\n' + m.group(2),
                                    template), encoding="utf-8")
        code, refused = self.gdbpy_run("style", path)
        if code != 2 or "SC01" not in (refused.get("error") or "") or refused.get("backend"):
            raise RuntimeError("style refusal: exit %d, %s" % (code, refused.get("error")))
        return "image check not needed in the emulator; scenario with a RAM address refused before the server"

    def gdbpy_outcomes(self):
        """Шаблон как есть и три его варианта: итог и код возврата различают FAIL, ERROR и TIMEOUT."""
        template = (GDBPY_SKILL / "assets" / "scenario_template.py").read_text(encoding="utf-8")
        section = re.compile(r"(    # -{10,} Сценарий\n).*?(    # -{20,}\n)", re.S)
        bodies = {"template": None,
                  "fail": '    check("main reached", reach("main"))\n'
                          '    check("deliberate failure", read("probe_state") == 0x5A)\n',
                  "error": '    check("main reached", reach("main"))\n'
                           '    write("probe_frame_period_ms", 7)\n    raise RuntimeError("deliberate error")\n',
                  "timeout": '    step("stuck in scenario")\n    __import__("time").sleep(120)\n'}
        expected = {"template": (0, "PASS"), "fail": (1, "FAIL"), "error": (2, "ERROR"), "timeout": (2, "TIMEOUT")}
        folder = OUT / "gdbpy-variants"
        folder.mkdir(parents=True, exist_ok=True)
        seen = []
        for name, body in bodies.items():
            text = template if body is None else section.sub(lambda m: m.group(1) + body + m.group(2), template)
            if body is not None and body not in text:
                raise RuntimeError("scenario section not found in the template")
            path = folder / ("%s.py" % name)
            path.write_text(text, encoding="utf-8")
            code, result = self.gdbpy_run(name, path, timeout=15 if name == "timeout" else 60)
            if (code, result["status"]) != expected[name]:
                raise RuntimeError("%s: exit %d, status %s" % (name, code, result["status"]))
            if name == "error" and result.get("restored") is not True:
                raise RuntimeError("written variable not restored after an exception")
            if name == "timeout" and result.get("last_step") != "stuck in scenario":
                raise RuntimeError("timeout without the last step: %s" % result.get("last_step"))
            seen.append("%s=%s" % (name, result["status"]))
        return ", ".join(seen) + "; restoration after ERROR verified"

    def diag_record(self):
        """stm32-diag-record end to end without a board: real resolver on the probe ELF, a fake
        OpenOCD telnet serving changing words, files checked by independent readers (mcap, rosbags)."""
        try:
            from mcap.reader import make_reader
            from rosbags.rosbag1 import Reader as BagReader
        except ImportError:
            raise Skip("python packages mcap and rosbags are needed to read the files back")
        sys.path.insert(0, str(ROOT / "ci"))
        from fake_openocd import FakeOpenOcd
        skill = ROOT / "stm32-diag-record" / "scripts"
        work = OUT / "diag"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        env = dict(self.env, GDB=self.gdb())
        spec = skill / "fields.toml"
        addresses = work / "addresses.json"
        run([sys.executable, "-B", skill / "resolve_addresses.py", "--elf", self.elves["F411CE"], "--spec", spec,
             "--svd", self.svd_f411, "--out", addresses], log=work / "resolve.log", env=env, timeout=120)
        fields = json.loads(addresses.read_text(encoding="utf-8"))["fields"]
        by_topic = {field["topic"]: field for field in fields}
        for consumer in ("stm32-ros-alt-channel", "stm32-diag-record"):
            run([sys.executable, "-B", ROOT / "ci/test_shared_debug_tools.py"],
                env=dict(self.env, SHARED_TEST_SKILL=consumer, PROBE_ADDRESSES=str(addresses)),
                log=work / (consumer + "-shared.log"))
        if len(fields) != 8 or by_topic["/probe/tick_ms"]["address"] != by_topic["/probe/uptime_s"]["address"]:
            raise RuntimeError("fields with the same expr must stay separate: %s" % [f["topic"] for f in fields])
        import struct as _struct
        memory = {
            by_topic["/probe/frames"]["address"]: lambda n: n + 1,
            by_topic["/probe/tick_ms"]["address"]: lambda n: 250 * n,
            by_topic["/probe/sine_milli"]["address"]: lambda n: (n % 21 - 10) * 100,
            by_topic["/probe/ramp"]["address"]: lambda n: _struct.unpack("<I", _struct.pack("<f", (n % 10) / 10.0))[0],
            by_topic["/probe/work_cycles"]["address"]: lambda n: 4000 + n,
            by_topic["/probe/loop_counter"]["address"]: lambda n: 1000 * n,
            by_topic["/probe/pin_frame"]["address"] & ~3: lambda n: (n % 2) << 15,
        }
        results = {}
        with FakeOpenOcd(memory) as server:
            for kind in ("mcap", "bag"):
                out = work / ("record.%s" % kind)
                run([sys.executable, "-B", skill / "record_diag.py", "--use-running", "--port", str(server.port),
                     "--elf", self.elves["F411CE"], "--svd", self.svd_f411, "--record", kind, "--seconds", "1",
                     "--rate", "20", "--out", out], log=work / ("record-%s.log" % kind), env=env, timeout=120)
                results[kind] = out
        values = {}
        with results["mcap"].open("rb") as handle:
            reader = make_reader(handle)
            channels = {channel.topic for channel in reader.get_summary().channels.values()}
            for _schema, channel, message in reader.iter_messages():
                values.setdefault(channel.topic, []).append(message.data)
        if channels != set(by_topic):
            raise RuntimeError("mcap channels %s" % sorted(channels))
        uint = lambda data: _struct.unpack("<I", data)[0]
        frames = [uint(d) for d in values["/probe/frames"]]
        ticks = [uint(d) for d in values["/probe/tick_ms"]]
        seconds = [uint(d) for d in values["/probe/uptime_s"]]
        ramp = [_struct.unpack("<f", d)[0] for d in values["/probe/ramp"]]
        pins = {uint(d) for d in values["/probe/pin_frame"]}
        if len(frames) < 10 or frames != sorted(set(frames)):
            raise RuntimeError("frames %s" % frames[:12])
        if seconds != [tick // 1000 for tick in ticks]:
            raise RuntimeError("uint32_seconds does not follow the tick")
        if not all(0.0 <= value <= 0.9 + 1e-6 for value in ramp) or pins - {0, 1}:
            raise RuntimeError("ramp %s, pins %s" % (ramp[:5], pins))
        with BagReader(results["bag"]) as bag:
            bag_topics = {connection.topic for connection in bag.connections}
            bag_messages = sum(1 for _ in bag.messages())
        if bag_topics != set(by_topic) or bag_messages < 8 * 10:
            raise RuntimeError("bag topics %s, messages %d" % (sorted(bag_topics), bag_messages))
        return "8 fields (two share an address), %d cycles; mcap and bag read back by mcap and rosbags" % len(frames)

    def diag_record_renode(self):
        """stm32-diag-record --backend renode against a running probe in Renode (real monitor, no fakes)."""
        try:
            from mcap.reader import make_reader
        except ImportError:
            raise Skip("python package mcap is needed to read the file back")
        import socket as _socket
        import struct as _struct
        work = OUT / "diag-renode"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        # A plain build: UART log, no semihosting exit, so the core keeps running during the recording.
        build = work / "probe"
        run(["cmake", "-S", self.probe, "-B", build, "-G", "Ninja", "-DBOARD=F411CE", "-DCMAKE_BUILD_TYPE=Debug",
             "-DPROBE_RENODE_TEST=OFF", "-DARM_TOOLCHAIN_ROOT=%s" % (self.tools / "gcc")],
            log=work / "configure.log", env=self.env)
        run(["cmake", "--build", build], log=work / "build.log", env=self.env)
        elf = build / "stm32_skills_probe.elf"
        with _socket.socket() as probe_port:
            probe_port.bind(("127.0.0.1", 0))
            port = probe_port.getsockname()[1]
        script = work / "run.resc"
        script.write_text("\n".join([
            'mach create "probe"',
            "machine LoadPlatformDescription @%s" % (self.probe / "emu" / "renode" / PROBE_MODELS["F411CE"]).as_posix(),
            "sysbus.usart1 CreateFileBackend @%s true" % (work / "uart.log").as_posix(),
            "sysbus LoadELF @%s" % elf.as_posix(), "start", ""]), encoding="utf-8")
        log = (work / "renode.log").open("w", encoding="utf-8")
        renode = subprocess.Popen([str(self.renode), "--disable-gui", "--plain", "--port", str(port),
                                   "-e", "include @%s" % script.as_posix()], stdout=log, stderr=subprocess.STDOUT)
        out = work / "record.mcap"
        try:
            deadline = time.monotonic() + 90
            while "Machine started" not in (work / "renode.log").read_text(encoding="utf-8", errors="replace"):
                if renode.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Renode did not start the machine (see renode.log)")
                time.sleep(0.5)
            doctor = run([sys.executable, "-B", ROOT / "stm32-diag-record" / "scripts" / "record_diag.py", "--backend",
                          "renode", "--port", str(port), "--elf", elf, "--svd", self.svd_f411, "--doctor"],
                         log=work / "doctor.log", env=dict(self.env, GDB=self.gdb()), timeout=120)
            if not re.search(r"Чтение памяти на ходу\s+ок", doctor):
                raise RuntimeError("doctor did not read memory from Renode: %s" % doctor[-300:])
            run([sys.executable, "-B", ROOT / "stm32-diag-record" / "scripts" / "record_diag.py", "--backend", "renode",
                 "--port", str(port), "--elf", elf, "--svd", self.svd_f411, "--record", "mcap", "--seconds", "2",
                 "--rate", "10", "--out", out], log=work / "record.log", env=dict(self.env, GDB=self.gdb()), timeout=120)
        finally:
            renode.terminate()
            try:
                renode.wait(timeout=10)
            except subprocess.TimeoutExpired:
                renode.kill()
            log.close()
        values = {}
        with out.open("rb") as handle:
            for _schema, channel, message in make_reader(handle).iter_messages():
                values.setdefault(channel.topic, []).append(_struct.unpack("<I", message.data)[0])
        validate_renode_record(values)
        ticks = values["/probe/tick_ms"]
        return "doctor reads memory; %d cycles from the Renode monitor; tick_ms %d -> %d" % (len(ticks), ticks[0], ticks[-1])

    def diag_record_rtt_timer(self):
        """rtt-timer end to end without a timer model: build the SRAM image, run the REAL compiled stub in
        Renode by software-pending the timer IRQ through the NVIC, parse the frames it wrote to the ring."""
        import struct as _struct
        rtt = ROOT / "stm32-diag-record" / "scripts" / "rtt"
        sys.path.insert(0, str(rtt))
        import rtt_frames as rf
        elf = self.elves["F411CE"]
        work = OUT / "diag-rtt"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        env = dict(self.env, GDB=self.gdb())
        # Спецификация задаётся здесь, а не из репозитория зонда: закреплённый коммит может не содержать
        # tools/diag-fields.toml. Поля — только символы RAM зонда (без svd:), поэтому SVD не нужен.
        spec = work / "fields.toml"
        spec.write_text("".join(
            '[[field]]\ntopic = "%s"\nmessage = "std_msgs/UInt32"\nkind = "uint32"\nexpr = "%s"\n\n' % pair
            for pair in (("/probe/frames", "&probe_telemetry.frames"), ("/probe/counter", "&probe_counter"),
                         ("/probe/tick_ms", "&platform_tick"), ("/probe/work", "&probe_telemetry.work_cycles"))),
            encoding="utf-8")
        addresses = work / "addresses.json"
        run([sys.executable, "-B", ROS_SKILL / "scripts" / "resolve_addresses.py", "--elf", elf, "--spec", spec,
             "--out", addresses], log=work / "resolve.log", env=env, timeout=120)
        tick = None
        for line in run([self.gcc / "arm-none-eabi-nm", elf]).splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[2] == "platform_tick":
                tick = int(parts[0], 16)
        if tick is None:
            raise RuntimeError("platform_tick not found in the probe ELF")
        # M0-стенд без VTOR образ собирать не должен.
        refusal = subprocess.run([sys.executable, "-B", str(rtt / "build_rtt_image.py"), "--elf", str(elf),
                                  "--addresses", str(addresses), "--board", "f030r8", "--toolchain", str(self.gcc),
                                  "--sram-base", "0x20001000", "--tick-address", "0x%08X" % tick,
                                  "--out", str(work / "no.bin")], capture_output=True, text=True)
        if refusal.returncode == 0 or "не поддержан" not in (refusal.stdout + refusal.stderr):
            raise RuntimeError("f030r8 (Cortex-M0) image was not refused")
        # Образ, выходящий за конец SRAM, должен отклоняться (иначе хвост ляжет в несуществующую память).
        overflow = subprocess.run([sys.executable, "-B", str(rtt / "build_rtt_image.py"), "--elf", str(elf),
                                   "--addresses", str(addresses), "--board", "f411ce", "--toolchain", str(self.gcc),
                                   "--sram-base", "0x2001E000", "--buffer", "16384", "--tick-address", "0x%08X" % tick,
                                   "--out", str(work / "big.bin")], capture_output=True, text=True)
        if overflow.returncode == 0 or "не умещается в SRAM" not in (overflow.stdout + overflow.stderr):
            raise RuntimeError("over-large image was not refused")
        base = 0x2001E000
        image = work / "rtt.bin"
        run([sys.executable, "-B", rtt / "build_rtt_image.py", "--elf", elf, "--addresses", addresses,
             "--board", "f411ce", "--toolchain", self.gcc, "--sram-base", "0x%08X" % base,
             "--tick-address", "0x%08X" % tick, "--rate", "1000", "--buffer", "4096", "--out", image],
            log=work / "build.log", env=env, timeout=120)
        layout = json.loads(image.with_suffix(".layout.json").read_text(encoding="utf-8"))
        # Доктор (офлайн-часть): факты из ELF зонда — вершина стека, конец данных, SystemCoreClock.
        import rtt_timer
        sp_top, ram_used, scc, scc_init = rtt_timer.elf_facts(elf, self.gcc)
        if sp_top != 0x20020000 or not (0x20000000 < ram_used < 0x20020000) or scc is None:
            raise RuntimeError("elf_facts: sp_top=%s ram_used=%s scc=%s" % (sp_top, ram_used, scc))
        cb, buf, vec = layout["rtt"]["control_block"], layout["rtt"]["buffer"], layout["vectors"]["address"]
        counters, flen, n = layout["counters"], layout["frame"]["len"], layout["frame"]["count"]
        iser, ispr, bit = layout["nvic"]["iser"], layout["nvic"]["ispr"], layout["nvic"]["bit"]
        pends = 8
        lines = ["using sysbus", 'mach create "probe"',
                 "machine LoadPlatformDescription @%s" % self.platform("F411CE").as_posix(),
                 "sysbus LoadELF @%s" % elf.as_posix(), 'emulation RunFor "0.20"',
                 "sysbus LoadBinary @%s 0x%08X" % (image.resolve().as_posix(), base),
                 "sysbus WriteDoubleWord 0x%08X 0x%08X" % (layout["vtor"], vec),
                 "sysbus WriteDoubleWord 0x%08X 0x%08X" % (iser, 1 << bit)]
        for _ in range(pends):
            lines += ["sysbus WriteDoubleWord 0x%08X 0x%08X" % (ispr, 1 << bit), 'emulation RunFor "0.01"']
        lines += ['echo "WROFF="', "sysbus ReadDoubleWord 0x%08X" % (cb + 36),
                  'echo "DROP="', "sysbus ReadDoubleWord 0x%08X" % (counters + 4)]
        words = (pends * flen) // 4
        for i in range(words):
            lines += ['echo "D%03d="' % i, "sysbus ReadDoubleWord 0x%08X" % (buf + 4 * i)]
        lines += ["quit", ""]
        script = work / "rtt.resc"
        script.write_text("\n".join(lines), encoding="utf-8")
        text = run([self.renode, "--disable-gui", "--console", "--plain", "-e", "include @%s" % script.as_posix()],
                   log=work / "renode.log", timeout=180)
        vals = {k: int(v, 16) for k, v in re.findall(r"^([A-Z0-9]+)=\s*\n\s*(0x[0-9A-Fa-f]+)", text, re.M)}
        if vals.get("DROP", -1) != 0 or vals.get("WROFF", -1) != pends * flen:
            raise RuntimeError("WrOff=%s drop=%s (ожидалось %d, 0)" % (vals.get("WROFF"), vals.get("DROP"), pends * flen))
        raw = b"".join(_struct.pack("<I", vals["D%03d" % i]) for i in range(words))
        reader = rf.FrameReader(layout)
        frames = reader.feed(raw)
        if len(frames) != pends or reader.stats["dropped_est"]:
            raise RuntimeError("кадров %d (ожидалось %d), потери %d" % (len(frames), pends, reader.stats["dropped_est"]))
        seqs = [f[0] for f in frames]
        ticks = [f[1] for f in frames]
        by_topic = {topic: value for topic, _m, value, _r in frames[-1][2]}
        if seqs != list(range(1, pends + 1)) or ticks != sorted(ticks):
            raise RuntimeError("seq %s, ticks %s" % (seqs, ticks))
        # Метка кадра = platform_tick; поле /probe/tick_ms читает тот же platform_tick -> должны совпасть.
        if by_topic["/probe/tick_ms"] != ticks[-1] or by_topic["/probe/frames"] < 1:
            raise RuntimeError("tick_ms %s vs frame tick %s" % (by_topic.get("/probe/tick_ms"), ticks[-1]))
        return ("real stub in Renode: %d frames, seq 1..%d, tick %d->%d, 0 drops; f030r8 and RAM-overflow refused; "
                "doctor elf_facts (SP 0x%08X, SystemCoreClock)" % (pends, pends, ticks[0], ticks[-1], sp_top))

    def list_signals(self):
        """--list-signals: глобальные переменные из ELF (GDB+DWARF) в CSV, только проверенные в GDB выражения.

        Зонд: раскрытие структуры, карта kind, адреса по nm, маска для полей короче слова, фильтр пути.
        Тестовый ELF: вложенные структуры и --depth, внутреннее пространство имён C++, одноимённые static
        из разных файлов, знаковое int16, отсев массивов/битовых полей/64-битного. Затем каждая строка CSV
        проходит resolve_addresses.py и обязана дать тот же адрес (CSV пригоден без доработки)."""
        import csv as _csv
        work = OUT / "list-signals"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        env = dict(self.env, GDB=self.gdb())
        record = ROOT / "stm32-diag-record" / "scripts" / "record_diag.py"

        def listing(elf, out, path, depth=None):
            command = [sys.executable, "-B", record, "--list-signals", "--elf", elf, "--signals-path", path,
                       "--out", str(out)] + (["--signals-depth", str(depth)] if depth is not None else [])
            text = run(command, log=work / (out.stem + ".log"), env=env, timeout=120)
            return {r["expr"]: r for r in _csv.DictReader(out.open(encoding="utf-8"))}, text

        def resolve_back(elf, rows, name):
            spec = work / (name + ".toml")
            spec.write_text("".join(
                '[[field]]\ntopic = "/t/%d"\nmessage = "%s"\nkind = "%s"\nexpr = "%s"\n%s\n'
                % (i, r["message"], r["kind"], r["expr"], ("mask = %s\n" % r["mask"]) if r["mask"] else "")
                for i, r in enumerate(rows.values())), encoding="utf-8")
            out = work / (name + ".json")
            run([sys.executable, "-B", ROS_SKILL / "scripts" / "resolve_addresses.py", "--elf", elf, "--spec", spec,
                 "--out", out], log=work / (name + "-resolve.log"), env=env, timeout=120)
            fields = json.loads(out.read_text(encoding="utf-8"))["fields"]
            bad = [(r["expr"], r["address"], hex(f["address"])) for r, f in zip(rows.values(), fields)
                   if int(r["address"], 16) != f["address"]]
            if bad:
                raise RuntimeError("CSV не совпал с resolve_addresses: %s" % bad)

        # 1. Зонд.
        elf = self.elves["F411CE"]
        rows, _text = listing(elf, work / "probe.csv", "probe.cpp")
        symbols = {}
        for line in run([self.gcc / "arm-none-eabi-nm", elf]).splitlines():
            parts = line.split()
            if len(parts) == 3:
                symbols[parts[2]] = int(parts[0], 16)
        expect = {"&probe_counter": "uint32", "&probe_telemetry.frames": "uint32",
                  "&probe_telemetry.ramp": "float32", "&probe_telemetry.sine_milli": "int32"}
        for expr, kind in expect.items():
            if expr not in rows or rows[expr]["kind"] != kind:
                raise RuntimeError("%s: %s, ожидался kind %s" % (expr, rows.get(expr), kind))
        if int(rows["&probe_counter"]["address"], 16) != symbols["probe_counter"]:
            raise RuntimeError("адрес probe_counter: %s, nm %#x" % (rows["&probe_counter"]["address"], symbols["probe_counter"]))
        if rows["&probe_telemetry.flags"]["mask"] != "0xFF" or rows["&probe_telemetry.samples"]["mask"] != "0xFFFF":
            raise RuntimeError("маски коротких полей: %s" % {k: v["mask"] for k, v in rows.items()})
        if any("probe.cpp" not in r["file"] for r in rows.values()):
            raise RuntimeError("фильтр по пути пропустил чужой файл: %s" % sorted(rows))
        resolve_back(elf, rows, "probe-back")
        probe_count = len(rows)

        # 2. Тестовый ELF с трудными случаями.
        src = work / "src"
        src.mkdir()
        (src / "main.cpp").write_text(
            "#include <stdint.h>\n"
            "struct Inner { uint32_t a; int16_t b; uint8_t c; };\n"
            "struct Outer { uint8_t flag; Inner in; float f; uint16_t arr[3]; uint32_t bits : 3; };\n"
            "volatile Outer g_outer;\n"
            "namespace { volatile uint32_t g_hidden; }\n"
            "static volatile int32_t s_twin = 1;\n"
            "volatile uint64_t g_wide;\n"
            "int twin_other();\n"
            "extern \"C\" void _start() { g_hidden = s_twin + twin_other() + g_outer.flag + (int)g_wide; for (;;) {} }\n",
            encoding="utf-8")
        (src / "twin.cpp").write_text("#include <stdint.h>\nstatic volatile int32_t s_twin = 2;\n"
                                      "int twin_other() { return s_twin; }\n", encoding="utf-8")
        test_elf = work / "signals-test.elf"
        run([self.gcc / "arm-none-eabi-g++", "-mcpu=cortex-m4", "-mthumb", "-g3", "-O0", "-nostdlib",
             "-fno-exceptions", "-fno-rtti", "-Wl,-Ttext=0x08000000", "-Wl,-Tdata=0x20000000", "-Wl,--entry=_start",
             src / "main.cpp", src / "twin.cpp", "-o", test_elf], log=work / "signals-test-build.log", timeout=120)
        deep, text = listing(test_elf, work / "test-d2.csv", "src/", depth=2)
        shallow, text1 = listing(test_elf, work / "test-d1.csv", "src/", depth=1)
        need = {"&g_outer.in.a": ("uint32", ""), "&g_outer.in.b": ("int32", "0xFFFF"), "&g_outer.flag": ("uint32", "0xFF"),
                "&g_outer.f": ("float32", ""), "&'(anonymous namespace)::g_hidden'": ("uint32", ""),
                "&'main.cpp'::s_twin": ("int32", ""), "&'twin.cpp'::s_twin": ("int32", "")}
        for expr, (kind, mask) in need.items():
            if expr not in deep or (deep[expr]["kind"], deep[expr]["mask"]) != (kind, mask):
                raise RuntimeError("%s: %s, ожидалось %s/%s (есть: %s)" % (expr, deep.get(expr), kind, mask, sorted(deep)))
        if "&s_twin" in deep or any(e in deep for e in ("&g_outer.arr", "&g_outer.bits", "&g_wide")):
            raise RuntimeError("лишние строки: %s" % sorted(deep))
        if any(e.startswith("&g_outer.in.") for e in shallow) or "структура глубже --depth 1" not in text1:
            raise RuntimeError("--depth 1 раскрыл вложенную структуру: %s" % sorted(shallow))
        for reason in ("массив", "битовое поле", "не читается одним словом"):
            if reason not in text:
                raise RuntimeError("в сводке нет причины «%s»" % reason)
        resolve_back(test_elf, deep, "test-back")
        return ("зонд: %d строк, адреса = nm, маски коротких полей; тест: --depth 1/2, anonymous namespace, "
                "static-двойники через 'файл'::, int16 с маской; все строки CSV = resolve_addresses" % probe_count)

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
            ("stm32-diag-record: RTT evidence independent readers", lambda: check_rtt_record(readers=True)),
            ("stm32-diag-record: record via fake OpenOCD", emulation.diag_record),
            ("stm32-diag-record: record from Renode monitor", emulation.diag_record_renode),
            ("stm32-diag-record: rtt-timer stub in Renode", emulation.diag_record_rtt_timer),
            ("stm32-diag-record: --list-signals from ELF", emulation.list_signals),
            ("stm32-gdbpy-scenario: probe scenario in Renode F411CE", emulation.gdbpy_probe),
            ("stm32-gdbpy-scenario: PASS/FAIL/ERROR/TIMEOUT outcomes", emulation.gdbpy_outcomes),
            ("stm32-gdbpy-scenario: image check skipped in Renode, style refusal", emulation.gdbpy_image_style),
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
