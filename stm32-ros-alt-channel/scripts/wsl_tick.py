#!/usr/bin/env python3
"""Показать tick ядра (adjtimex). Ожидается 10000 (100 Гц).

Если tick другой, гостевое время WSL уходит и периодически подводится — отсюда паузы в записях ROS.
Запуск: wsl -d Ubuntu-20.04 -u root -e python3 /mnt/.../scripts/wsl_tick.py
"""
import ctypes


class Timex(ctypes.Structure):
    _fields_ = [("modes", ctypes.c_int), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long),
                ("tolerance", ctypes.c_long), ("time", ctypes.c_long * 2),
                ("tick", ctypes.c_long)]


value = Timex()
ctypes.CDLL("libc.so.6", use_errno=True).adjtimex(ctypes.byref(value))
print("tick = %d %s" % (value.tick, "ок" if abs(value.tick - 10000) <= 5 else "НЕВЕРНО, ожидается 10000"))
