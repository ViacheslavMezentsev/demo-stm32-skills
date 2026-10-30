"""Поиск GDB с Python; явный путь, GDB, toolchain/bin и PATH."""
import os
import pathlib
import shutil
import subprocess

GDB_NAMES = ("arm-none-eabi-gdb-py3", "arm-none-eabi-gdb", "gdb-multiarch")


def gdb_has_python(gdb):
    try:
        result = subprocess.run([gdb, "--nx", "--batch", "-ex", "python print('STM32_GDB_PYTHON_READY')"],
                                capture_output=True, text=True, timeout=15)
        return result.returncode == 0 and "STM32_GDB_PYTHON_READY" in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def find_gdb(explicit=None, toolchain=None):
    selected = explicit or os.environ.get("GDB")
    if selected:
        if gdb_has_python(str(selected)):
            return str(selected)
        raise SystemExit("указанный GDB недоступен или не поддерживает Python: %s" % selected)
    root = toolchain or os.environ.get("STM32_TOOLCHAIN_PATH")
    candidates = []
    for name in GDB_NAMES:
        if root:
            folder = pathlib.Path(root)
            for directory in (folder / "bin", folder):
                found = shutil.which(name, path=str(directory))
                if found:
                    candidates.append(found)
        found = shutil.which(name)
        if found:
            candidates.append(found)
    checked = []
    for candidate in dict.fromkeys(candidates):
        if gdb_has_python(candidate):
            return candidate
        checked.append("  %s → без Python (в xPack Python есть только у arm-none-eabi-gdb-py3)" % candidate)
    raise SystemExit("не найден GDB с Python; задайте GDB=<путь к arm-none-eabi-gdb-py3>. Проверено:\n%s"
                     % ("\n".join(checked) or "  в PATH и --toolchain нет %s" % ", ".join(GDB_NAMES)))
