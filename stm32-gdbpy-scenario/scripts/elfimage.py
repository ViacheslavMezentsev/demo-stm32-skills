"""ELF прошивки без сторонних библиотек: что должно лежать во Flash, build-id и таблица символов.

Используется запускателем (сверка образа перед сценарием, scripts/image_check.py внутри GDB) и
проверкой сценария (scripts/check_scenario.py). Только ELF32 little-endian — так собирают Cortex-M.

Во Flash сверяются секции, а не сегменты: программатор пишет содержимое секций, а промежутки
между ними внутри сегмента могут остаться стёртыми (0xFF), хотя в файле ELF там нули.
"""
import hashlib
import struct

SHT_NOBITS, SHT_NOTE, SHT_SYMTAB = 8, 7, 2
SHF_ALLOC = 0x2
PT_LOAD = 1
# Область кода Cortex-M (0x00000000–0x1FFFFFFF): Flash, системная память, ITCM. ОЗУ (0x20000000+)
# после сброса не совпадает с ELF и в сверку не входит.
CODE_REGION_END = 0x20000000


class ElfError(ValueError):
    pass


class Elf(object):
    def __init__(self, path):
        self.path = str(path)
        with open(path, "rb") as stream:
            self.data = stream.read()
        data = self.data
        if data[:4] != b"\x7fELF":
            raise ElfError("не ELF: %s" % path)
        if data[4] != 1 or data[5] != 1:
            raise ElfError("нужен ELF32 little-endian (Cortex-M): %s" % path)
        (self.phoff, self.shoff, _, _, self.phentsize, self.phnum, self.shentsize, self.shnum,
         self.shstrndx) = struct.unpack_from("<IIIHHHHHH", data, 28)
        self.segments = [struct.unpack_from("<8I", data, self.phoff + i * self.phentsize) for i in range(self.phnum)]
        raw = [struct.unpack_from("<10I", data, self.shoff + i * self.shentsize) for i in range(self.shnum)]
        names = raw[self.shstrndx] if self.shnum else None
        self.sections = []
        for item in raw:
            name = self._string(names[4] + item[0]) if names else ""
            self.sections.append({"name": name, "type": item[1], "flags": item[2], "addr": item[3],
                                  "offset": item[4], "size": item[5], "link": item[6]})

    def _string(self, offset):
        end = self.data.index(b"\0", offset)
        return self.data[offset:end].decode("utf-8", "replace")

    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()

    def lma(self, section):
        """Адрес загрузки секции (LMA) по сегменту, в который она входит; у .data он во Flash."""
        for p_type, p_offset, p_vaddr, p_paddr, p_filesz, _, _, _ in self.segments:
            if p_type == PT_LOAD and p_filesz and p_offset <= section["offset"] < p_offset + p_filesz:
                return p_paddr + (section["offset"] - p_offset)
        return section["addr"]

    def flash_sections(self):
        """[(имя, LMA, байты)] — содержимое, которое прошивка держит в области кода."""
        out = []
        for section in self.sections:
            if not section["flags"] & SHF_ALLOC or section["type"] == SHT_NOBITS or not section["size"]:
                continue
            address = self.lma(section)
            if address < CODE_REGION_END:
                body = self.data[section["offset"]:section["offset"] + section["size"]]
                out.append((section["name"], address, body))
        return sorted(out, key=lambda item: item[1])

    def build_id(self):
        """(адрес, байты) build-id в области кода или None (сборка без -Wl,--build-id)."""
        for section in self.sections:
            if section["type"] != SHT_NOTE or not section["flags"] & SHF_ALLOC:
                continue
            offset, end = section["offset"], section["offset"] + section["size"]
            while offset + 12 <= end:
                namesz, descsz, kind = struct.unpack_from("<III", self.data, offset)
                desc = offset + 12 + ((namesz + 3) & ~3)
                if kind == 3 and self.data[offset + 12:offset + 12 + namesz] == b"GNU\0":
                    address = self.lma(section) + (desc - section["offset"])
                    if address < CODE_REGION_END:
                        return address, self.data[desc:desc + descsz]
                offset = desc + ((descsz + 3) & ~3)
        return None

    def symbols(self):
        """Имена из .symtab (объекты, функции, без секционных и файловых записей)."""
        names = set()
        for section in self.sections:
            if section["type"] != SHT_SYMTAB:
                continue
            strtab = self.sections[section["link"]]
            for offset in range(section["offset"], section["offset"] + section["size"], 16):
                name, _, _, info, _, _ = struct.unpack_from("<IIIBBH", self.data, offset)
                if name and (info & 0xF) in (0, 1, 2):
                    names.add(self._string(strtab["offset"] + name))
        return names


def compare(read, sections, chunk=4096):
    """Сверить секции с памятью цели. read(адрес, длина) -> bytes.

    Возвращает {"status": "match"|"mismatch", "checked_bytes", "sections", "first_diff"}.
    """
    checked = 0
    for name, address, body in sections:
        for start in range(0, len(body), chunk):
            expected = body[start:start + chunk]
            actual = read(address + start, len(expected))
            if actual != expected:
                index = next(i for i in range(len(expected)) if i >= len(actual) or actual[i] != expected[i])
                return {"status": "mismatch", "checked_bytes": checked + index, "sections": len(sections),
                        "first_diff": {"section": name, "address": "0x%08X" % (address + start + index),
                                       "expected": expected[index:index + 8].hex(),
                                       "actual": actual[index:index + 8].hex()}}
            checked += len(expected)
    return {"status": "match", "checked_bytes": checked, "sections": len(sections), "first_diff": None}
