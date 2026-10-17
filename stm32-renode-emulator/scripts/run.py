"""Run the firmware boot log (F411/QEMU or G474/Renode), optionally verify it.

    python scripts/run.py --profile G474 --elf build/firmware.elf
    python scripts/run.py --profile G474 --elf build/firmware.elf --verify --expected boot.toml
    python scripts/run.py --profile F411 --elf build/firmware.elf --qemu qemu-system-arm

The G474 platform defaults to assets/stm32g4_mock.repl of this skill (--platform overrides it).
Every attempt gets a new output directory with startup.log, emulator.log and run.json.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import uuid

SKILL = Path(__file__).resolve().parents[1]
DEFAULT_PLATFORM = SKILL / "assets" / "stm32g4_mock.repl"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import renode_compat

# Политика версий Renode. Основная версия — 1.17: в ней семихостинг переехал на отдельный обработчик
# (`CPU.SemihostingHandler`, путь `cpu.semihosting.uart`), и именно на ней идёт вся дальнейшая работа,
# включая GDB-сервер. Версия 1.16.1 поддерживается для совместимости (макет преобразуется слоем
# `renode_compat`), но помечена к выводу из эксплуатации.
PRIMARY_VERSION = (1, 17, 0)
COMPATIBLE_VERSIONS = ((1, 16, 1),)
# Каталог второй версии в Docker-образе, где рядом установлены 1.16.1 (в PATH) и 1.17.0.
SECOND_VERSION_PATH = "/opt/renode-1.17.0/renode"


def executable(explicit, variable, default):
    selected = explicit or os.environ.get(variable) or default
    found = shutil.which(selected)
    if not found and default == "renode" and not explicit and not os.environ.get(variable):
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Renode/renode.exe"
        if candidate.is_file():
            found = str(candidate)
    if not found:
        raise ValueError(f"Executable not found: {selected} ({variable})")
    return found


def find_renode(explicit):
    """Найти Renode, предпочитая основную версию 1.17.

    Порядок поиска:
      1. явный `--renode` или `RENODE_BINARY` — их выбор не оспаривается;
      2. версия 1.17 из второго каталога образа (`/opt/renode-1.17.0/renode`), если она есть;
      3. то, что нашлось в `PATH` / Program Files.

    Возвращает (путь, разобранная версия). Ошибка, если версия не определилась: слой совместимости
    должен знать, под какую версию готовить макет, и «на глаз» это не угадывается.
    """
    if explicit or os.environ.get("RENODE_BINARY"):
        candidate = explicit or os.environ["RENODE_BINARY"]
        binary = executable(candidate, "RENODE_BINARY", candidate)
        return binary, renode_compat.parse_version_of(binary)
    # Явный поиск основной версии: каталог второй версии в образе.
    secondary = Path(SECOND_VERSION_PATH)
    if secondary.is_file():
        version = renode_compat.parse_version_of(str(secondary))
        if version == PRIMARY_VERSION:
            return str(secondary), version
    binary = executable(None, "RENODE_BINARY", "renode")
    return binary, renode_compat.parse_version_of(binary)


def check_version(version):
    """Проверить, что версия допустима, и предупредить о совместимости при 1.16."""
    if version is None:
        raise ValueError("Cannot determine Renode version; see --renode / RENODE_BINARY")
    if version == PRIMARY_VERSION:
        return
    if version in COMPATIBLE_VERSIONS:
        print(f"WARNING: Renode {'.'.join(map(str, version))} is maintained for compatibility only; "
              f"the main version is {'.'.join(map(str, PRIMARY_VERSION))}", file=sys.stderr)
        return
    raise ValueError(f"Unsupported Renode version {'.'.join(map(str, version))}: expected "
                     f"{'.'.join(map(str, PRIMARY_VERSION))} (or compatible 1.16.1)")


def renode_path(path):
    value = Path(path).resolve().as_posix()
    if any(c in value for c in ('"', '\n', '\r')):
        raise ValueError("Unsupported character in Renode path")
    return '@' + value.replace(' ', '\\ ')


def command(profile, binary, elf, output, renode_version=None, platform_file=None):
    if profile == "F411":
        # File chardev separates semihosting from emulator diagnostics.
        log = (output / "startup.log").as_posix().replace(',', ',,')
        return [binary, '-M', 'netduinoplus2', '-nographic', '-monitor', 'none',
                '-serial', 'none', '-no-reboot', '-chardev', f'file,id=boot,path={log}',
                '-semihosting-config', 'enable=on,target=native,chardev=boot', '-kernel', str(elf)]
    platform = output / "platform.repl"
    platform.write_text(renode_compat.platform_text(
        Path(platform_file or DEFAULT_PLATFORM).read_text(encoding="utf-8"),
        renode_version), encoding="utf-8")
    script = output / "boot.resc"
    script.write_text('\n'.join([
        'logLevel 3', 'using sysbus', 'mach create "board"',
        'machine LoadPlatformDescription ' + renode_path(platform),
        renode_compat.uart_path(renode_version) + ' CreateFileBackend ' + renode_path(output / 'startup.log') + ' true',
        'sysbus LoadELF ' + renode_path(elf), 'emulation RunFor "1"', 'quit', ''
    ]), encoding='utf-8')
    return [binary, '--disable-gui', '--console', '--plain', '-e', 'include ' + renode_path(script)]


def run_process(argv, logfile, timeout):
    with logfile.open('wb') as log:
        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=(os.name != 'nt'))
        try:
            return process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=log, stderr=subprocess.STDOUT, timeout=10)
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=10)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['F411', 'G474'], required=True)
    parser.add_argument('--elf', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='New directory; never reuse a previous attempt')
    parser.add_argument('--qemu')
    parser.add_argument('--renode')
    parser.add_argument('--objcopy', default='arm-none-eabi-objcopy')
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--platform', type=Path, help='Renode REPL for G474 (default: assets/stm32g4_mock.repl)')
    parser.add_argument('--verify', action='store_true', help='Also validate the boot log against ELF and expected TOML')
    parser.add_argument('--expected', type=Path, help='Expected boot log TOML for --verify (see assets/expected.example.toml)')
    args = parser.parse_args(argv)
    timeout = args.timeout if args.timeout is not None else (20 if args.profile == 'F411' else 40)
    if not 0 < timeout <= 300:
        parser.error('timeout must be finite and in (0, 300] seconds')
    if args.verify and not args.expected:
        parser.error('--verify needs --expected <toml>')
    elf = args.elf.resolve()
    output = (args.output or elf.parent / 'emulation' / (
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 2
    result = dict(schema=1, profile=args.profile, elf=str(elf), verified=False,
                  emulator="qemu" if args.profile == "F411" else "renode",
                  started_utc=datetime.now(timezone.utc).isoformat(),
                  status='ERROR', process_code=None, command_code=2, timeout_s=timeout)
    try:
        result['elf_sha256'] = hashlib.sha256(elf.read_bytes()).hexdigest()
        binary = executable(args.qemu, 'QEMU_BINARY', 'qemu-system-arm') if args.profile == 'F411' else None
        version = None
        if args.profile == 'G474':
            binary, version = find_renode(args.renode)
            check_version(version)
            result['renode_version'] = '.'.join(map(str, version))
        cmd = command(args.profile, binary, elf, output, version, args.platform)
        result['command'] = cmd
        result['process_code'] = run_process(cmd, output / 'emulator.log', timeout)
        boot = output / 'startup.log'
        if boot.exists():
            print(boot.read_text(encoding='utf-8', errors='replace'), end='', flush=True)
        if result['process_code'] != 0:
            raise ValueError(f"Emulator exited with {result['process_code']}; see emulator.log")
        if not boot.is_file() or not boot.stat().st_size:
            raise ValueError('No firmware output; see emulator.log')
        result.update(status='CAPTURED', command_code=0)
        if args.verify:
            check = [sys.executable, str(SKILL / 'scripts' / 'check_startup_log.py'),
                     '--elf', str(elf), '--log', str(boot),
                     '--expected', str(args.expected.resolve()),
                     '--report', str(output / 'startup-log-report.json'), '--objcopy', args.objcopy]
            code = run_process(check, output / 'verification.log', 60)
            result.update(verification_code=code, verified=True,
                          status='PASS' if code == 0 else 'FAIL', command_code=0 if code == 0 else 1)
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        result.update(status='ERROR', command_code=2, error=str(error) or type(error).__name__)
        print(f"ERROR: {result['error']}", file=sys.stderr)
    finally:
        (output / 'run.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f"\n{result['status']}: {output}", file=sys.stderr)
    return result['command_code']


if __name__ == '__main__':
    sys.exit(main())
