#!/usr/bin/env python3
"""weact_la — обёртка sigrok-cli для WeAct LogicAnalyzerV1 (CY7C68013A/CBM9002A, 8 каналов, до 24 МГц).

Только стандартная библиотека Python ≥ 3.8; нужен установленный sigrok-cli (драйвер fx2lafw).

    python3 weact_la.py doctor                 # проверить, что анализатор готов к работе
    python3 weact_la.py capture out.sr --rate 8m --time 0.5 [--trigger D7=r]
    python3 weact_la.py info out.sr            # частота, длина, фронты по каналам
    python3 weact_la.py channels out.sr --bitrate 1000000      # уровни и короткие импульсы по каналам
    python3 weact_la.py can out.sr --rx D1 [--dronecan] [--csv frames.csv]   # кадры CAN со временем
    python3 weact_la.py can-role out.sr --line D1 --other D0   # второй канал — линия или TX узла?

    from weact_la import LogicAnalyzer
    la = LogicAnalyzer(samplerate="8m")
    cap = la.capture("run.sr", duration=0.2, trigger={7: "r"})
    print(cap.pulse_stats(0), cap.uart(1, 115200))
"""
import configparser
try:
    import grp
except ImportError:      # Windows: группа не нужна, права проверяются иначе
    grp = None
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import zipfile

__all__ = ["LogicAnalyzer", "Capture", "doctor", "LAError", "TriggerTimeout", "parse_rate"]

USB_IDS = {
    ("0925", "3881"): "WeAct LogicAnalyzerV1 (EEPROM как Saleae Logic; ID не меняется и после загрузки fx2lafw)",
    ("1d50", "608c"): "FX2-плата с ID sigrok fx2lafw",
    ("04b4", "8613"): "CY7C68013A без EEPROM-идентификатора (пустой FX2)",
}
FIRMWARE = "fx2lafw-saleae-logic.fw"
FW_DIRS = ("/usr/share/sigrok-firmware", "/usr/local/share/sigrok-firmware")
FX2_RATES = (20e3, 25e3, 50e3, 100e3, 200e3, 250e3, 500e3, 1e6, 2e6, 3e6, 4e6, 6e6,
             8e6, 12e6, 16e6, 24e6, 48e6)
RELIABLE_MAX_RATE = 24e6        # 48 МГц драйвер предлагает, но по USB 2.0 на 8 каналах не держит


class LAError(RuntimeError):
    """Ошибка анализатора или sigrok-cli."""


class TriggerTimeout(LAError):
    """Триггер не сработал за отведённое время."""


# ---------------------------------------------------------------- утилиты

def parse_rate(rate):
    """'8m', '500k', '1.5MHz', 8000000 -> 8000000.0 (Гц)."""
    if isinstance(rate, (int, float)):
        return float(rate)
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgG]?)\s*(?:[hH][zZ])?\s*", str(rate))
    if not m:
        raise ValueError(f"не понимаю частоту {rate!r}")
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]


def fmt_rate(hz):
    """8000000 -> '8m' (формат, который понимает sigrok-cli)."""
    for div, suf in ((1e6, "m"), (1e3, "k")):
        if hz >= div and hz % div == 0:
            return f"{int(hz // div)}{suf}"
    return str(int(hz))


def _chname(ch):
    """0 -> 'D0'; 'D3' -> 'D3'."""
    return f"D{ch}" if isinstance(ch, int) else str(ch)


def _trigger_spec(trigger):
    """{7: 'r'} / 'D7=r' / [(0,'f'),(1,'1')] -> 'D7=r' (условия через запятую — логическое И)."""
    if trigger is None:
        return None
    if isinstance(trigger, str):
        return trigger
    items = trigger.items() if isinstance(trigger, dict) else trigger
    parts = []
    for ch, cond in items:
        if cond not in ("0", "1", "r", "f", "e"):
            raise ValueError(f"условие триггера {cond!r}: допустимо 0 1 r f e")
        parts.append(f"{_chname(ch)}={cond}")
    return ",".join(parts)


# ---------------------------------------------------------------- диагностика

def _usb_devices():
    base = "/sys/bus/usb/devices"
    found = []
    if not os.path.isdir(base):
        return found
    for d in os.listdir(base):
        p = os.path.join(base, d)
        try:
            vid = open(os.path.join(p, "idVendor")).read().strip()
            pid = open(os.path.join(p, "idProduct")).read().strip()
        except OSError:
            continue
        if (vid, pid) in USB_IDS:
            bus = int(open(os.path.join(p, "busnum")).read())
            dev = int(open(os.path.join(p, "devnum")).read())
            found.append((vid, pid, f"/dev/bus/usb/{bus:03d}/{dev:03d}"))
    return found


def doctor(sigrok="sigrok-cli", verbose=True):
    """Проверяет цепочку «ПО → USB → права → прошивка → sigrok видит устройство».

    Возвращает список (проверка, ok, подробности); ok всего списка — все True.
    """
    res = []

    def add(name, ok, detail):
        res.append((name, ok, detail))
        if verbose:
            print(f"[{'OK' if ok else '!!'}] {name}: {detail}")

    exe = shutil.which(sigrok)
    add("sigrok-cli", bool(exe), exe or "не найден: sudo apt install sigrok-cli sigrok-firmware-fx2lafw")
    if exe:
        v = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.splitlines()
        add("версия", True, v[0] if v else "?")

    fw = [os.path.join(d, FIRMWARE) for d in FW_DIRS if os.path.exists(os.path.join(d, FIRMWARE))]
    add("прошивка fx2lafw", bool(fw), fw[0] if fw else f"нет {FIRMWARE}: sudo apt install sigrok-firmware-fx2lafw")

    devs = _usb_devices()
    add("USB-устройство", bool(devs),
        "; ".join(f"{v}:{p} {USB_IDS[(v, p)]} ({n})" for v, p, n in devs) or
        "не найдено: проверьте кабель/порт (без хаба), lsusb должен показать 0925:3881 или 1d50:608c")
    for v, p, node in devs:
        ok = os.access(node, os.R_OK | os.W_OK)
        try:
            st = os.stat(node)
            owner = f"{oct(st.st_mode & 0o777)} группа {grp.getgrgid(st.st_gid).gr_name}"
        except (OSError, KeyError):
            owner = "?"
        add(f"права {node}", ok, owner + ("" if ok else " — нет доступа: udev-правило + группа plugdev, переподключить"))

    try:
        # Windows не имеет групп POSIX: проверка прав неприменима.
        if grp is None or not hasattr(os, "getgroups"):
            in_group = True
        else:
            in_group = "plugdev" in [grp.getgrgid(g).gr_name for g in os.getgroups()]
    except KeyError:
        in_group = False
    add("группа plugdev", in_group, "есть" if in_group else "нет в текущей сессии: sudo usermod -aG plugdev $USER и перелогиниться")

    if exe:
        r = subprocess.run([exe, "-d", "fx2lafw", "--scan"], capture_output=True, text=True, timeout=30)
        line = next((l for l in r.stdout.splitlines() if l.startswith("fx2lafw")), "")
        add("sigrok видит анализатор", bool(line), line.strip() or (r.stderr.strip() or "fx2lafw не найден"))
    return res


# ---------------------------------------------------------------- запись

class Capture:
    """Готовая запись (.sr): чтение отсчётов без sigrok-cli, измерения, декодеры."""

    def __init__(self, path, sigrok="sigrok-cli"):
        self.path = str(path)
        self.sigrok = sigrok
        with zipfile.ZipFile(self.path) as z:
            meta = configparser.ConfigParser()
            meta.read_string(z.read("metadata").decode())
            dev = meta["device 1"]
            self.samplerate = parse_rate(dev["samplerate"].replace(" ", ""))
            self.unitsize = int(dev.get("unitsize", "1"))
            prefix = dev.get("capturefile", "logic-1")
            # probeN=<имя>: N — номер бита + 1 (сохраняется и при записи части каналов)
            self.channels = {v: int(k[5:]) - 1 for k, v in dev.items() if k.startswith("probe")}
            chunks = sorted((n for n in z.namelist() if n == prefix or n.startswith(prefix + "-")),
                            key=lambda n: int(n.rsplit("-", 1)[1]) if n != prefix else 0)
            self.data = b"".join(z.read(n) for n in chunks)
        if self.unitsize != 1:
            raise LAError(f"unitsize={self.unitsize}: поддержаны только 8 каналов (1 байт на отсчёт)")
        self._runs = {}

    # -- базовое
    @property
    def n(self):
        return len(self.data)

    @property
    def duration(self):
        return self.n / self.samplerate

    def t(self, sample):
        """Номер отсчёта -> секунды от начала записи."""
        return sample / self.samplerate

    def _bit(self, ch):
        name = _chname(ch)
        if name in self.channels:
            return self.channels[name]
        if isinstance(ch, int) and str(ch) in self.channels:   # файлы из -I binary называют каналы 0..7
            return self.channels[str(ch)]
        if isinstance(ch, int) and 0 <= ch < 8:
            return ch
        raise KeyError(f"канала {ch!r} нет в записи: {sorted(self.channels)}")

    def runs(self, ch):
        """[(уровень, начало, длина)] — серии одинакового уровня; C-скорость через translate+regex."""
        b = self._bit(ch)
        if b not in self._runs:
            table = bytes((ord("1") if (i >> b) & 1 else ord("0")) for i in range(256))
            s = self.data.translate(table).decode("ascii")
            self._runs[b] = [(int(m.group()[0]), m.start(), m.end() - m.start())
                             for m in re.finditer(r"0+|1+", s)]
        return self._runs[b]

    def level(self, ch, sample=0):
        return (self.data[sample] >> self._bit(ch)) & 1

    def edges(self, ch, kind="e"):
        """Номера отсчётов фронтов: kind 'r' — фронт 0→1, 'f' — спад 1→0, 'e' — любой."""
        out = []
        for lvl, start, _ in self.runs(ch)[1:]:
            if kind == "e" or (kind == "r" and lvl == 1) or (kind == "f" and lvl == 0):
                out.append(start)
        return out

    def edge_times(self, ch, kind="e"):
        return [self.t(s) for s in self.edges(ch, kind)]

    def first_edge(self, ch, kind="r", after=0.0):
        """Время первого фронта после `after` секунд или None."""
        a = round(after * self.samplerate)
        return next((self.t(s) for s in self.edges(ch, kind) if s >= a), None)

    def pulses(self, ch, level=1):
        """Длительности ПОЛНЫХ импульсов уровня `level` (секунды); обрезанные края отбрасываются."""
        r = self.runs(ch)
        return [ln / self.samplerate for lvl, _, ln in r[1:-1] if lvl == level]

    def periods(self, ch, kind="r"):
        e = self.edges(ch, kind)
        return [(b - a) / self.samplerate for a, b in zip(e, e[1:])]

    @staticmethod
    def stats(values):
        if not values:
            return {"n": 0}
        mean = sum(values) / len(values)
        return {"n": len(values), "min": min(values), "max": max(values), "mean": mean,
                "jitter": max(values) - min(values)}

    def pulse_stats(self, ch, level=1):
        return self.stats(self.pulses(ch, level))

    def frequency(self, ch):
        p = self.periods(ch, "r")
        return len(p) / sum(p) if p else None

    def duty(self, ch):
        """Средняя скважность по полным периодам (доля времени в 1)."""
        e = self.edges(ch, "r")
        if len(e) < 2:
            return None
        hi = sum(ln for lvl, s, ln in self.runs(ch) if lvl == 1 and e[0] <= s < e[-1])
        return hi / (e[-1] - e[0])

    def delay(self, ch_from, ch_to, edge_from="r", edge_to="r", after=0.0):
        """Задержка от первого фронта ch_from (после `after`) до следующего фронта ch_to."""
        t0 = self.first_edge(ch_from, edge_from, after)
        if t0 is None:
            return None
        t1 = self.first_edge(ch_to, edge_to, t0)
        return None if t1 is None else t1 - t0

    def count(self, ch, kind="r"):
        return len(self.edges(ch, kind))

    # -- декодеры sigrok
    def decode(self, decoder, annotation=None, **opts):
        """Запускает декодер sigrok. Каналы опций (rx=0, clk=2…) задаются номерами или именами.

        Возвращает [(t_start, t_end, текст)]. Пример: decode('uart', 'rx-data', rx=0, baudrate=115200)
        """
        chan_opts = {"rx", "tx", "clk", "mosi", "miso", "cs", "scl", "sda", "data", "can_rx", "dq"}
        parts = [decoder]
        for k, v in opts.items():
            if k in chan_opts:
                v = self._name_for_bit(self._bit(v))
            parts.append(f"{k}={v}")
        cmd = [self.sigrok, "-i", self.path, "-P", ":".join(parts), "--protocol-decoder-samplenum"]
        if annotation:
            cmd += ["-A", f"{decoder}={annotation}"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise LAError(r.stderr.strip())
        out = []
        for line in r.stdout.splitlines():
            m = re.match(r"(\d+)-(\d+)\s+\S+?:\s*(.*)", line)
            if m:
                out.append((self.t(int(m.group(1))), self.t(int(m.group(2))), m.group(3)))
        return out

    def channel_report(self, bitrate=None):
        """По каждому каналу: доля единиц, фронты, импульсы низкого уровня; при bitrate — доля однобитных.

        Первый шаг разбора записи шины: живой ли канал и на что он похож (линия, TX узла, помеха),
        до декодера, который на чужом сигнале выдаёт правдоподобный мусор.
        """
        rows = []
        for name, b in sorted(self.channels.items(), key=lambda kv: kv[1]):
            runs = self.runs(b)
            ones = sum(ln for lvl, _, ln in runs if lvl == 1)
            low = [ln / self.samplerate for lvl, _, ln in runs[1:-1] if lvl == 0]
            row = {"channel": name, "high": ones / self.n if self.n else 0, "edges": max(0, len(runs) - 1),
                   "low_pulses": len(low), "low_min": min(low) if low else None, "low_max": max(low) if low else None}
            if bitrate and low:
                bit = 1.0 / bitrate
                row["single_bit"] = sum(1 for x in low if x < 1.5 * bit) / len(low)
                row["longest_bits"] = max(low) / bit
            rows.append(row)
        return rows

    def can_frames(self, rx, bitrate=1000000):
        """Кадры CAN декодером sigrok: [{t, sample, id, ext, data, ack, warnings}].

        ack — уровень линии rx в слоте подтверждения: 'dominant' (кадр кто-то принял) или 'recessive'.
        Время — по номерам отсчётов (--protocol-decoder-samplenum), а не по меткам декодера.
        """
        cmd = [self.sigrok, "-i", self.path, "-P", "can:can_rx=%s:nominal_bitrate=%d"
               % (self._name_for_bit(self._bit(rx)), bitrate), "-A",
               "can=sof:id:full-id:data:ack-slot:warnings", "--protocol-decoder-samplenum"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise LAError(r.stderr.strip())
        frames, bit = [], self._bit(rx)
        for line in r.stdout.splitlines():
            m = re.match(r"(\d+)-(\d+)\s+\S+?:\s*(.*)", line)
            if not m:
                continue
            start, end, text = int(m.group(1)), int(m.group(2)), m.group(3)
            if text == "Start of frame":
                frames.append({"t": self.t(start), "sample": start, "id": None, "ext": False, "data": [],
                               "ack": None, "warnings": []})
                continue
            if not frames:
                continue
            frame = frames[-1]
            ident = re.match(r"(Full )?Identifier: \d+ \((0x[0-9a-fA-F]+)\)", text)
            data = re.match(r"Data byte \d+: (0x[0-9a-fA-F]+)", text)
            if ident:
                if ident.group(1) or frame["id"] is None:
                    frame["id"], frame["ext"] = int(ident.group(2), 16), bool(ident.group(1))
            elif data:
                frame["data"].append(int(data.group(1), 16))
            elif text.startswith("ACK slot"):
                middle = (start + end) // 2
                frame["ack"] = "dominant" if not (self.data[middle] >> bit) & 1 else "recessive"
                frame["ack_range"] = (start, end)
            elif not text.startswith("Extended Identifier"):
                frame["warnings"].append(text)
        return frames

    def can_role(self, line, other, bitrate=1000000):
        """Чем является other рядом с линией line: TX узла (подтверждения + свои кадры) или та же линия.

        На выходе приёмника трансивера (линия) видны все кадры. На TX узла — только его кадры и одиночные
        биты ACK, которыми он подтверждает чужие кадры; декодер принимает такой бит за начало кадра и даёт
        0x1FFFFFFF — это не помеха, а неверно выбранный канал для декодера.
        """
        frames = [f for f in self.can_frames(line, bitrate) if f.get("ack_range") and not f["warnings"]]
        b = self._bit(other)
        acked = sum(1 for f in frames
                    if any(not (self.data[s] >> b) & 1 for s in range(f["ack_range"][0], f["ack_range"][1] + 1)))
        own = [f for f in self.can_frames(other, bitrate) if f["id"] is not None and not f["warnings"] and f["data"]]
        on_line = {(f["id"], tuple(f["data"])) for f in frames}
        own_on_line = sum(1 for f in own if (f["id"], tuple(f["data"])) in on_line)
        if len(own) > len(frames):
            own_set = {(f["id"], tuple(f["data"])) for f in own}
            own_on_line = sum(1 for key in on_line if key in own_set)
        if frames and len(own) > 1.1 * len(frames) and own_on_line >= 0.9 * len(frames):
            verdict = "%s — линия (на нём больше кадров), а %s похож на TX узла: каналы перепутаны" % (other, line)
        elif frames and own and own_on_line == len(own) and len(own) >= 0.9 * len(frames):
            verdict = "та же линия"
        elif frames and acked >= 0.9 * (len(frames) - len(own)):
            verdict = "TX узла: подтверждает чужие кадры и передаёт свои"
        else:
            verdict = "не похоже ни на линию, ни на TX узла этой шины"
        return {"line_frames": len(frames), "acked_by_other": acked, "other_own_frames": len(own),
                "other_own_on_line": own_on_line, "other_ids": sorted({f["id"] for f in own}), "verdict": verdict}

    @staticmethod
    def dronecan(ident):
        """Разбор 29-битного ID по раскладке DroneCAN (UAVCAN v0)."""
        src, priority = ident & 0x7F, ident >> 24
        if ident & 0x80:
            return {"kind": "service", "priority": priority, "type": (ident >> 16) & 0xFF,
                    "request": bool(ident & 0x8000), "dst": (ident >> 8) & 0x7F, "src": src}
        return {"kind": "message", "priority": priority, "dtid": (ident >> 8) & 0xFFFF, "src": src}

    def _name_for_bit(self, b):
        return next((name for name, bit in self.channels.items() if bit == b), f"D{b}")

    def uart(self, rx, baudrate, **opts):
        """Байты, принятые по линии rx (8N1 по умолчанию)."""
        rows = self.decode("uart", "rx-data", rx=rx, baudrate=baudrate, format="hex", **opts)
        return bytes(int(txt, 16) for _, _, txt in rows if re.fullmatch(r"[0-9A-Fa-f]{2}", txt))

    def spi(self, clk, mosi=None, miso=None, cs=None, **opts):
        """Список (mosi, miso) байтов (None, если линия не задана)."""
        kw = dict(clk=clk, **opts)
        for k, v in (("mosi", mosi), ("miso", miso), ("cs", cs)):
            if v is not None:
                kw[k] = v
        res = {}
        for line_name, ann in (("mosi", "mosi-data"), ("miso", "miso-data")):
            if kw.get(line_name) is not None:
                res[line_name] = [int(t, 16) for _, _, t in self.decode("spi", ann, **kw)
                                  if re.fullmatch(r"[0-9A-Fa-f]{2}", t)]
        n = max((len(v) for v in res.values()), default=0)
        return [tuple(res[k][i] if k in res and i < len(res[k]) else None for k in ("mosi", "miso"))
                for i in range(n)]

    def i2c(self, scl, sda):
        """События I²C по времени: 'Start', 'Address write: 3C', 'ACK', 'Data write: AF', 'NACK', 'Stop'…"""
        ann = "start:repeat-start:stop:ack:nack:address-read:address-write:data-read:data-write"
        rows = self.decode("i2c", ann, scl=scl, sda=sda)
        return [txt for _, _, txt in sorted(rows, key=lambda r: r[0])]

    def i2c_transactions(self, scl, sda):
        """[{'addr': 0x3C, 'read': False, 'data': [0x00, 0xAF], 'nack': False}, …] — по одной на Start…Stop/Sr."""
        out, cur = [], None
        for ev in self.i2c(scl, sda):
            if ev in ("Start", "Start repeat"):
                if cur:
                    out.append(cur)
                cur = {"addr": None, "read": False, "data": [], "nack": False}
            elif cur is None:
                continue
            elif ev.startswith("Address"):
                cur["addr"] = int(ev.split(":")[1], 16)
                cur["read"] = "read" in ev
            elif ev.startswith("Data"):
                cur["data"].append(int(ev.split(":")[1], 16))
            elif ev == "NACK":
                cur["nack"] = True
            elif ev == "Stop":
                out.append(cur)
                cur = None
        if cur:
            out.append(cur)
        return out

    def export(self, fmt="csv", out=None):
        """Экспорт в csv/vcd/bits/ascii через sigrok-cli; возвращает путь."""
        out = out or os.path.splitext(self.path)[0] + "." + fmt.split(":")[0]
        r = subprocess.run([self.sigrok, "-i", self.path, "-O", fmt, "-o", out], capture_output=True, text=True)
        if r.returncode != 0:
            raise LAError(r.stderr.strip())
        return out

    def summary(self):
        lines = [f"{self.path}: {self.samplerate / 1e6:g} МГц, {self.n} отсчётов, {self.duration * 1e3:.3f} мс"]
        for name, b in sorted(self.channels.items(), key=lambda kv: kv[1]):
            e = self.edges(b)
            f = self.frequency(b)
            lines.append(f"  {name}: старт={self.level(b)} фронтов={len(e)}"
                         + (f" f≈{f:.6g} Гц скважность≈{self.duty(b):.3f}" if f else ""))
        return "\n".join(lines)


class LogicAnalyzer:
    """Управление записью через sigrok-cli.

    samplerate — '8m', '1m', 24e6…; channels — номера 0..7 или имена 'D0'…;
    driver — 'fx2lafw' (WeAct), для отладки без железа — 'demo'; conn — '7.3' (шина.устройство),
    если анализаторов несколько.
    """

    def __init__(self, samplerate="8m", channels=range(8), driver="fx2lafw", conn=None, sigrok="sigrok-cli"):
        self.rate = parse_rate(samplerate)
        if driver == "fx2lafw":
            if self.rate not in FX2_RATES:
                raise ValueError(f"fx2lafw не умеет {samplerate}; допустимо: "
                                 + ", ".join(fmt_rate(r) for r in FX2_RATES))
            if self.rate > RELIABLE_MAX_RATE:
                print("weact_la: 48 МГц на 8 каналах по USB 2.0 нестабильно, рекомендуется ≤ 24 МГц", file=sys.stderr)
        self.channels = [_chname(c) for c in channels]
        self.driver = driver + (f":conn={conn}" if conn else "")
        self.sigrok = sigrok
        self.proc = None

    def scan(self):
        r = subprocess.run([self.sigrok, "-d", self.driver, "--scan"], capture_output=True, text=True, timeout=30)
        return any(l.startswith(self.driver.split(":")[0]) for l in r.stdout.splitlines())

    def _cmd(self, path, samples, trigger, pretrigger):
        cfg = f"samplerate={fmt_rate(self.rate)}"
        cmd = [self.sigrok, "-d", self.driver, "--channels", ",".join(self.channels),
               "--samples", str(int(samples)), "-o", str(path)]
        spec = _trigger_spec(trigger)
        if spec:
            cfg += f":captureratio={int(pretrigger)}"
            cmd += ["--triggers", spec, "--wait-trigger"]
        return cmd + ["--config", cfg]

    def arm(self, path, duration=None, samples=None, trigger=None, pretrigger=10, settle=1.0):
        """Взвести запись в фоне. duration (с) или samples; trigger: {7: 'r'} / 'D7=r'.

        settle — пауза на загрузку прошивки и настройку USB (первый запуск после подключения ~1–2 с).
        """
        if self.proc and self.proc.poll() is None:
            raise LAError("запись уже идёт")
        if samples is None:
            if duration is None:
                raise ValueError("нужно duration или samples")
            samples = round(duration * self.rate)
        if int(samples) != samples or samples <= 0:
            raise ValueError("samples должно быть положительным целым числом")
        self.expected_samples = int(samples)
        self.path = str(path)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        if os.path.exists(self.path):
            os.remove(self.path)
        self.proc = subprocess.Popen(self._cmd(self.path, samples, trigger, pretrigger),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(settle)
        if self.proc.poll() not in (None, 0):
            raise LAError(f"sigrok-cli завершился сразу: {self.proc.stderr.read().strip()}")
        return self

    def collect(self, timeout=10.0):
        """Дождаться окончания записи и вернуть Capture. Таймаут — TriggerTimeout (запись отменяется)."""
        if not self.proc:
            raise LAError("сначала arm()")
        try:
            _, err = self.proc.communicate(timeout=timeout)
            rc = self.proc.returncode
        except subprocess.TimeoutExpired:
            self.abort()
            raise TriggerTimeout(f"запись не завершилась за {timeout} с (триггер не сработал?)")
        err = err.strip()
        self.proc = None
        if rc == 0 and not os.path.exists(self.path):
            raise TriggerTimeout("запись завершилась, а триггер так и не сработал (данных нет)")
        if rc != 0 or not os.path.exists(self.path):
            raise LAError(f"sigrok-cli rc={rc}: {err or 'файл не создан'}")
        try:
            cap = Capture(self.path, self.sigrok)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, configparser.Error) as exc:
            raise LAError(f"повреждённая запись {self.path}: {exc}") from exc
        if cap.n != self.expected_samples or cap.samplerate != self.rate:
            raise LAError(
                f"неполная или несоответствующая запись: получено {cap.n} отсчётов "
                f"при {cap.samplerate:g} Гц, ожидалось {self.expected_samples} "
                f"при {self.rate:g} Гц; файл сохранён: {self.path}; {err}")
        if err:
            print(f"weact_la: {err}", file=sys.stderr)
        return cap

    def abort(self):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":
                self.proc.terminate()  # SIGINT не поддерживается Popen.send_signal на Windows.
            else:
                self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.communicate()
        self.proc = None

    def capture(self, path, duration=None, samples=None, trigger=None, pretrigger=10, timeout=10.0, settle=0.0):
        """Синхронная запись: взвести и дождаться."""
        return self.arm(path, duration, samples, trigger, pretrigger, settle).collect(timeout)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.abort()


# ---------------------------------------------------------------- CLI

def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    c = sub.add_parser("capture")
    c.add_argument("out")
    c.add_argument("--rate", default="8m")
    c.add_argument("--time", type=float, default=0.5, help="секунды")
    c.add_argument("--trigger", help="например D7=r или D0=f,D1=1")
    c.add_argument("--pre", type=int, default=10, help="претриггер, %%")
    c.add_argument("--timeout", type=float, default=10)
    c.add_argument("--driver", default="fx2lafw")
    i = sub.add_parser("info")
    i.add_argument("file")
    ch = sub.add_parser("channels")
    ch.add_argument("file")
    ch.add_argument("--bitrate", type=float, help="скорость линии: доля однобитных импульсов")
    cn = sub.add_parser("can")
    cn.add_argument("file")
    cn.add_argument("--rx", default="D0", help="канал линии (выход приёмника трансивера)")
    cn.add_argument("--bitrate", type=int, default=1000000)
    cn.add_argument("--dronecan", action="store_true", help="разбирать 29-битные ID по раскладке DroneCAN")
    cn.add_argument("--csv", help="записать кадры в CSV")
    cr = sub.add_parser("can-role")
    cr.add_argument("file")
    cr.add_argument("--line", default="D1")
    cr.add_argument("--other", default="D0")
    cr.add_argument("--bitrate", type=int, default=1000000)
    a = ap.parse_args(argv)
    if a.cmd == "doctor":
        return 0 if all(ok for _, ok, _ in doctor()) else 1
    if a.cmd == "capture":
        la = LogicAnalyzer(a.rate, driver=a.driver)
        cap = la.capture(a.out, duration=a.time, trigger=a.trigger, pretrigger=a.pre, timeout=a.timeout)
        print(cap.summary())
        return 0
    if a.cmd == "info":
        print(Capture(a.file).summary())
        return 0
    if a.cmd == "channels":
        cap = Capture(a.file)
        print(f"{cap.path}: {cap.samplerate / 1e6:g} МГц, {cap.duration:.3f} с")
        for row in cap.channel_report(a.bitrate):
            extra = (f", однобитных {row['single_bit'] * 100:.0f} %, самый длинный {row['longest_bits']:.1f} бит"
                     if "single_bit" in row else "")
            lows = (f", низкие {row['low_min'] * 1e6:.2f}–{row['low_max'] * 1e6:.2f} мкс" if row["low_pulses"] else "")
            print(f"  {row['channel']}: единиц {row['high'] * 100:.2f} %, фронтов {row['edges']}, "
                  f"импульсов низкого уровня {row['low_pulses']}{lows}{extra}")
        return 0
    if a.cmd == "can":
        cap = Capture(a.file)
        frames = cap.can_frames(a.rx, a.bitrate)
        counts = {}
        for f in frames:
            key = "ошибка: " + f["warnings"][0] if f["warnings"] else ("0x%08X" % f["id"] if f["ext"] else "0x%03X" % f["id"])
            counts[key] = counts.get(key, 0) + 1
        good = [f for f in frames if not f["warnings"]]
        print(f"{cap.path}: кадров {len(frames)}, без замечаний {len(good)}, ACK доминантный у "
              f"{sum(1 for f in good if f['ack'] == 'dominant')}")
        for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            ident = int(key, 16) if key.startswith("0x") and len(key) == 10 else None
            print(f"  {key} ×{n}" + (f"  {Capture.dronecan(ident)}" if a.dronecan and ident is not None else ""))
        if a.csv:
            import csv
            with open(a.csv, "w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["t_s", "id", "ext", "data", "ack", "warnings"])
                for f in frames:
                    writer.writerow([f"{f['t']:.6f}", "" if f["id"] is None else hex(f["id"]), int(f["ext"]),
                                     " ".join("%02X" % x for x in f["data"]), f["ack"] or "", "; ".join(f["warnings"])])
            print(f"CSV: {a.csv}")
        return 0
    if a.cmd == "can-role":
        result = Capture(a.file).can_role(a.line, a.other, a.bitrate)
        print(f"{a.other} рядом с линией {a.line}: {result['verdict']}")
        print(f"  кадров на линии {result['line_frames']}, из них подтверждены импульсом на {a.other}: "
              f"{result['acked_by_other']}; своих кадров на {a.other}: {result['other_own_frames']} "
              f"(есть на линии: {result['other_own_on_line']}), ID: {', '.join(hex(i) for i in result['other_ids'])}")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except TriggerTimeout as e:
        print(f"ТАЙМАУТ: {e}", file=sys.stderr)
        sys.exit(124)
    except (LAError, ValueError) as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        sys.exit(2)
