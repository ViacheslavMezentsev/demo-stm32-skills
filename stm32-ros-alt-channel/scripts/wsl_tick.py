#!/usr/bin/env python3
"""Read-only adjtimex for 64-bit Linux/WSL. No clock settings are changed."""
import ctypes
import os

class Timex(ctypes.Structure):
    _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long), ("tolerance", ctypes.c_long),
                ("time", ctypes.c_long * 2), ("tick", ctypes.c_long), ("ppsfreq", ctypes.c_long),
                ("jitter", ctypes.c_long), ("shift", ctypes.c_int), ("stabil", ctypes.c_long),
                ("jitcnt", ctypes.c_long), ("calcnt", ctypes.c_long), ("errcnt", ctypes.c_long),
                ("stbcnt", ctypes.c_long), ("tai", ctypes.c_int), ("padding", ctypes.c_int * 11)]

def read_timex():
    if os.name != "posix" or ctypes.sizeof(ctypes.c_long) != 8:
        raise RuntimeError("нужен 64-битный Linux/WSL")
    value = Timex()  # modes=0: query only
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    libc.adjtimex.argtypes = [ctypes.POINTER(Timex)]
    libc.adjtimex.restype = ctypes.c_int
    if libc.adjtimex(ctypes.byref(value)) < 0:
        raise OSError(ctypes.get_errno(), "adjtimex")
    return {"tick": value.tick, "freq_ppm": value.freq / 65536.0, "status": value.status}

if __name__ == "__main__":
    import json
    print(json.dumps(read_timex()))
