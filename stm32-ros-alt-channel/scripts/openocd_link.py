"""Общий транспорт OpenOCD без ROS: telnet и доступ к памяти."""
import re
import socket

CLUSTER_BYTES = 256
WORD_LINE = re.compile(r"^0x([0-9a-fA-F]+):((?:\s+[0-9a-fA-F]{8})+)", re.M)


class Telnet(object):
    """Минимальный клиент telnet OpenOCD: команда -> текст ответа до приглашения '> '."""

    def __init__(self, host, port, timeout=2.0):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(timeout)
        self.buffer = b""
        self._read_until_prompt()  # приветствие и согласование telnet

    @staticmethod
    def _clean(data):
        # Последовательности согласования telnet (IAC = 0xFF) и управляющие символы не нужны.
        data = re.sub(rb"\xff[\xfb-\xfe].|\xff[\xf0-\xfa]", b"", data)
        return data.decode("utf-8", "replace").replace("\r", "").replace("\x00", "")

    def _read_until_prompt(self):
        while True:
            text = self._clean(self.buffer)
            position = text.rfind("\n> ")
            if position >= 0 or text.startswith("> ") or text.endswith("> "):
                self.buffer = b""
                return text
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                self.buffer = b""
                return None
            if not chunk:
                raise ConnectionError("OpenOCD закрыл соединение")
            self.buffer += chunk

    @staticmethod
    def _render(text):
        # OpenOCD стирает строку ввода забоями (\b), чтобы напечатать асинхронное сообщение, и потом
        # перепечатывает приглашение и ввод. Применяем забои, как терминал, чтобы найти эхо целиком.
        out = []
        for char in text:
            if char == "\b":
                if out:
                    out.pop()
            else:
                out.append(char)
        return "".join(out)

    def command(self, line):
        """Ответ на команду: текст после эха этой команды до следующего приглашения '> '.

        OpenOCD рассылает асинхронные сообщения во все сеансы telnet со своим приглашением — например,
        'error during read: Bad file descriptor', когда замечает закрытое чужое соединение (так выглядит
        проверка готовности порта в start_openocd на Windows). Если считать ответом всё до первого
        приглашения, ответы сдвигаются на одну команду и mdw возвращает чужое значение. Поэтому ответ
        отсчитывается от эха команды, а текст до эха (асинхронные сообщения) отбрасывается."""
        self.buffer = b""
        self.sock.sendall((line + "\n").encode("ascii"))
        data = b""
        while True:
            text = self._render(self._clean(data))
            echo = text.find(line)
            if echo >= 0:
                rest = text[echo + len(line):]
                end = rest.find("\n> ")
                if end >= 0:
                    return rest[:end].strip("\n")
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("OpenOCD закрыл соединение")
            data += chunk

    def close(self):
        self.sock.close()


def read_words(telnet, addresses):
    """Слова по выровненным адресам: соседние читаются одной командой mdw. Возвращает {адрес: слово}."""
    words = {}
    pending = sorted(set(addresses))
    while pending:
        start = pending[0]
        group = [a for a in pending if a - start < CLUSTER_BYTES]
        pending = pending[len(group):]
        count = (group[-1] - start) // 4 + 1
        text = telnet.command("mdw 0x%08X %d" % (start, count))
        if not text:
            continue
        for match in WORD_LINE.finditer(text):
            base = int(match.group(1), 16)
            for index, value in enumerate(match.group(2).split()):
                words[base + 4 * index] = int(value, 16)
    return words


class OpenOcdReader(object):
    """Чтение слов через telnet OpenOCD (mdw)."""

    def __init__(self, host, port):
        self.telnet = Telnet(host, port)

    def read_words(self, addresses):
        return read_words(self.telnet, addresses)

    def close(self):
        self.telnet.close()


def mdw(telnet, address):
    """Прочитать слово по адресу через OpenOCD. Разбирает строку '0x...: значение', игнорируя
    асинхронные сообщения и управляющие символы (telnet OpenOCD любит их подмешивать)."""
    text = telnet.command("mdw 0x%08X" % address) or ""
    # Значение принимается только из строки с тем же адресом: чужой ответ не выдаётся за наш.
    match = re.search(r"0x0*%x:\s*([0-9a-f]{1,8})\b" % address, text, re.IGNORECASE)
    if match:
        return int(match.group(1), 16)
    raise RuntimeError("mdw 0x%08X не вернул значение: %r" % (address, text[-60:]))


def mww(telnet, address, value):
    telnet.command("mww 0x%08X 0x%08X" % (address, value & 0xFFFFFFFF))


def set_bits(telnet, address, mask):
    mww(telnet, address, mdw(telnet, address) | mask)
