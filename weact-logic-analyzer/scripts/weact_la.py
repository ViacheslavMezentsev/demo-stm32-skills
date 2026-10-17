#!/usr/bin/env python3
"""weact_la — обёртка sigrok-cli для WeAct LogicAnalyzerV1 (CY7C68013A/CBM9002A, 8 каналов, до 24 МГц).

Только стандартная библиотека Python ≥ 3.8; нужен установленный sigrok-cli (драйвер fx2lafw).

    python3 weact_la.py doctor                 # проверить, что анализатор готов к работе
    python3 weact_la.py capture out.sr --rate 8m --time 0.5 [--trigger D7=r]
    python3 weact_la.py info out.sr            # частота, длина, фронты по каналам

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
            rc = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.abort()
            raise TriggerTimeout(f"запись не завершилась за {timeout} с (триггер не сработал?)")
        err = self.proc.stderr.read().strip()
        self.proc = None
        if rc == 0 and not os.path.exists(self.path):
            raise TriggerTimeout("запись завершилась, а триггер так и не сработал (данных нет)")
        if rc != 0 or not os.path.exists(self.path):
            raise LAError(f"sigrok-cli rc={rc}: {err or 'файл не создан'}")
        if err and "only sent" not in err:
            print(f"weact_la: {err}", file=sys.stderr)
        return Capture(self.path, self.sigrok)

    def abort(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
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


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except TriggerTimeout as e:
        print(f"ТАЙМАУТ: {e}", file=sys.stderr)
        sys.exit(124)
    except (LAError, ValueError) as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        sys.exit(2)
