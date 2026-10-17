"""Разрешение имён из SVD (CMSIS System View Description) в адреса.

ЗАЧЕМ. Для маршрута через OpenOCD читать можно не только переменные прошивки, но и **регистры
периферии**, а их имена удобнее брать из SVD-файла MCU (например, `resources/*.svd` проекта), чем
держать в голове сырые адреса.

СИНТАКСИС (используется в `fields.toml` как `expr = "svd:..."`):

    svd:ADC1.DR          — регистр: адрес = base периферии + addressOffset
    svd:GPIOA.ODR        — то же для GPIO
    svd:ADC1.SR.EOC      — поле регистра: кроме адреса даёт сдвиг и маску

Результат:
    адрес, размер регистра в битах, сдвиг поля (0, если поле не указано), маска поля (все единицы,
    если поле не указано)

Проверка: `python svd_lookup.py ADC1.DR --svd resources/STM32F411.svd`.
SVD по умолчанию — из переменной окружения STM32_SVD.
"""
import argparse
import os
import pathlib
import sys
import xml.etree.ElementTree as ElementTree

DEFAULT_SVD = os.environ.get("STM32_SVD")


class SvdLookup:
    """Минимальный разбор SVD: периферии, регистры, поля."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        if not self.path.is_file():
            raise SystemExit("нет SVD-файла: %s" % self.path)
        tree = ElementTree.parse(self.path)
        root = tree.getroot()
        self.peripherals = {}
        for peripheral in root.iter("peripheral"):
            name = peripheral.findtext("name")
            base_text = peripheral.findtext("baseAddress")
            if not name or not base_text:
                continue
            registers = {}
            for register in peripheral.iter("register"):
                reg_name = register.findtext("name")
                offset_text = register.findtext("addressOffset")
                if not reg_name or not offset_text:
                    continue
                size = int(register.findtext("size") or "32", 0)
                fields = {}
                for field in register.iter("field"):
                    field_name = field.findtext("name")
                    if not field_name:
                        continue
                    bit_offset = int(field.findtext("bitOffset") or "0", 0)
                    bit_width = int(field.findtext("bitWidth") or "1", 0)
                    fields[field_name] = (bit_offset, bit_width)
                registers[reg_name] = {"offset": int(offset_text, 0), "size": size,
                                       "fields": fields}
            self.peripherals[name] = {"base": int(base_text, 0), "registers": registers}

    def resolve(self, path):
        """`ADC1.DR` или `ADC1.SR.EOC` -> (адрес, размер, сдвиг, маска)."""
        parts = path.split(".")
        if len(parts) < 2:
            raise ValueError("ожидается ПЕРИФЕРИЯ.РЕГИСТР[.ПОЛЕ], получено: %s" % path)
        peripheral_name, register_name = parts[0], parts[1]
        field_name = parts[2] if len(parts) > 2 else None

        peripheral = self.peripherals.get(peripheral_name)
        if peripheral is None:
            raise ValueError("нет периферии %s в %s" % (peripheral_name, self.path.name))
        register = peripheral["registers"].get(register_name)
        if register is None:
            raise ValueError("нет регистра %s.%s" % (peripheral_name, register_name))

        address = peripheral["base"] + register["offset"]
        shift, mask = 0, 0xFFFFFFFF
        if field_name:
            field = register["fields"].get(field_name)
            if field is None:
                raise ValueError("нет поля %s.%s.%s" % (peripheral_name, register_name, field_name))
            shift, width = field
            mask = ((1 << width) - 1) << shift
        return address, register["size"], shift, mask


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="имя вида ADC1.DR или ADC1.SR.EOC")
    parser.add_argument("--svd", default=DEFAULT_SVD, help="SVD-файл MCU (по умолчанию $STM32_SVD)")
    args = parser.parse_args()
    if not args.svd:
        parser.error("укажите --svd или переменную окружения STM32_SVD")

    lookup = SvdLookup(args.svd)
    address, size, shift, mask = lookup.resolve(args.path)
    print("SVD:        %s" % lookup.path.name)
    print("имя:        %s" % args.path)
    print("адрес:      0x%08X" % address)
    print("размер:     %d бит" % size)
    if shift or mask != 0xFFFFFFFF:
        print("поле:       сдвиг %d, маска 0x%08X (значение = (слово & маска) >> %d)"
              % (shift, mask, shift))
    return 0


if __name__ == "__main__":
    sys.exit(main())
