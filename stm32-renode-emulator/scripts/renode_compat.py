"""Различия версий Renode, важные для макета и сценариев прогона.

В 1.16.1 вывод семихостинга вешается прямо на процессор, а журнал пишется в устройство
`sysbus.cpu.uart`. В 1.17 появился отдельный обработчик `CPU.SemihostingHandler`, UART вешается на
него, а путь устройства стал `cpu.semihosting.uart`. Один общий макет обе версии обслужить не может:
1.16.1 не знает типа `CPU.SemihostingHandler`, а 1.17 не принимает UART на процессоре. Поэтому
макет для прогона готовится копией в форме нужной версии.
"""

import re
import subprocess
from pathlib import Path

OLD_CPU_UART = 'uart: UART.SemihostingUart @ cpu'
NEW_CPU_UART = ('semihosting: CPU.SemihostingHandler @ cpu\n'
                'uart: UART.SemihostingUart @ semihosting')
OLD_UART_PATH = 'sysbus.cpu.uart'
NEW_UART_PATH = 'cpu.semihosting.uart'


def parse_version(text):
    """Версия из строки вида «Renode, version 1.17.0 (…)» или None."""
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', text or '')
    return tuple(int(part) for part in match.groups()) if match else None


def uses_semihosting_handler(version):
    """Нужен ли отдельный обработчик семихостинга (1.17 и новее)."""
    return version is None or version >= (1, 17, 0)


def platform_text(text, version):
    """Текст макета в форме, понятной указанной версии Renode."""
    if uses_semihosting_handler(version):
        return text if NEW_CPU_UART in text else text.replace(OLD_CPU_UART, NEW_CPU_UART)
    return text if OLD_CPU_UART in text else text.replace(NEW_CPU_UART, OLD_CPU_UART)


def uart_path(version):
    """Путь устройства UART для файлового бэкенда журнала."""
    return NEW_UART_PATH if uses_semihosting_handler(version) else OLD_UART_PATH


def detect_version(binary):
    """Версия установленного Renode; None, если определить не удалось."""
    try:
        result = subprocess.run([str(binary), '--version'], capture_output=True, text=True,
                                timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version((result.stdout or '') + (result.stderr or ''))


def parse_version_of(binary):
    """Версия Renode по пути к исполняемому файлу: разбор `--version` или имени каталога.

    Если прибор не отвечает на `--version` (например, обёртка), версия пробуется из пути: в образе
    вторая версия лежит в каталоге вида `/opt/renode-1.17.0`. Это запасной путь, а не основной:
    определять версию по имени каталога разрешено только когда запуск не удался.
    """
    version = detect_version(binary)
    if version is not None:
        return version
    return parse_version(str(binary))
