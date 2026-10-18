#!/usr/bin/env python3
"""Поддельный telnet OpenOCD для проверки записи без платы (stm32-diag-record, маршрут A).

Отвечает на `mdw <адрес> [число]` словами из памяти, которую задаёт вызывающий код, повторяет
команду эхом и печатает приглашение `> `, как настоящий OpenOCD; в начале сеанса шлёт байты
согласования telnet. По четыре слова в строке ответа — чтобы проверить разбор многострочного вывода.

    from fake_openocd import FakeOpenOcd
    with FakeOpenOcd({0x20000000: lambda n: n}) as server:   # значение = номер чтения
        ... record_diag.py --use-running --port server.port ...
"""
import re
import socket
import threading

MDW = re.compile(r"^mdw\s+(0x[0-9a-fA-F]+|\d+)(?:\s+(\d+))?\s*$")


class FakeOpenOcd(object):
    def __init__(self, memory):
        """memory: {выровненный адрес: функция(номер чтения этого адреса) -> 32-битное слово}."""
        self.memory = memory
        self.reads = {}
        self.commands = []
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.stopped = False

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc_info):
        self.stopped = True
        self.server.close()

    def word(self, address):
        count = self.reads.get(address, 0)
        self.reads[address] = count + 1
        source = self.memory.get(address)
        return (source(count) if source else 0) & 0xFFFFFFFF

    def _answer(self, line):
        self.commands.append(line)
        match = MDW.match(line)
        if not match:
            return "invalid command name \"%s\"\r\n" % line.split()[0] if line.split() else ""
        address = int(match.group(1), 0)
        if address % 4:
            return "Error: address not aligned\r\n"
        count = int(match.group(2) or 1)
        rows = []
        for row in range(0, count, 4):
            base = address + 4 * row
            words = [self.word(base + 4 * i) for i in range(min(4, count - row))]
            rows.append("0x%08x: %s \r\n" % (base, " ".join("%08x" % w for w in words)))
        return "".join(rows)

    def _serve(self):
        while not self.stopped:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(connection,), daemon=True).start()

    def _client(self, connection):
        with connection:
            connection.sendall(b"\xff\xfb\x03\xff\xfb\x01\xff\xfd\x03\xff\xfd\x01Open On-Chip Debugger\r\n> ")
            buffer = b""
            while True:
                try:
                    data = connection.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buffer += data
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        connection.sendall(b"> ")
                        continue
                    reply = line + "\r\n" + self._answer(line) + "> "
                    connection.sendall(reply.encode("utf-8"))
