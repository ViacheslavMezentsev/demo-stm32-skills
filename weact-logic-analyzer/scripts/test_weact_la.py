"""Проверка weact_la без железа: синтетические сигналы (-I binary) и драйвер demo."""
import os, subprocess, sys, time, tempfile
sys.path.insert(0, os.path.dirname(__file__))
from weact_la import Capture, LogicAnalyzer, LAError, TriggerTimeout, parse_rate, doctor

RATE = 4_000_000
D = tempfile.mkdtemp()


class Sig:
    def __init__(self, n):
        self.buf = bytearray([0xFF] * n)   # все линии в 1 (как подтянутые входы)

    def set(self, ch, a, b, v):
        a, b = int(round(a)), int(round(b))
        m = 1 << ch
        for i in range(a, b):
            self.buf[i] = (self.buf[i] | m) if v else (self.buf[i] & ~m)


def build():
    n = RATE // 50            # 20 мс
    s = Sig(n)
    # ch0: UART 115200 "OK\r\n" с 1 мс
    bit = RATE / 115200
    t = RATE * 0.001
    for byte in b"OK\r\n":
        for v in [0] + [(byte >> i) & 1 for i in range(8)] + [1]:
            s.set(0, t, t + bit, v); t += bit
        t += bit
    # ch1: PWM 2 кГц, 25 %
    per = RATE / 2000
    k = 0
    while (k + 1) * per <= n:
        s.set(1, k * per, k * per + per * 0.25, 1); s.set(1, k * per + per * 0.25, (k + 1) * per, 0); k += 1
    s.set(1, k * per, n, 0)
    # ch2 clk, ch3 mosi, ch4 cs: SPI mode 0, 500 кГц, байты A5 3C
    half = RATE / 1_000_000
    t = RATE * 0.005
    s.set(2, 0, n, 0)
    s.set(4, t - 4 * half, t, 0)
    for byte in (0xA5, 0x3C):
        for i in range(7, -1, -1):
            s.set(3, t, t + 2 * half, (byte >> i) & 1)
            s.set(4, t, t + 2 * half, 0)
            s.set(2, t + half, t + 2 * half, 1)
            t += 2 * half
    s.set(4, t, t + 4 * half, 0)
    # ch5 scl, ch6 sda: I2C 100 кГц, запись 0x3C: 00 AF
    q = RATE / 400_000        # четверть периода
    t = RATE * 0.010
    s.set(6, t, t + q, 0); t += q                      # START: SDA↓ при SCL=1
    s.set(5, t, t + q, 0)

    def bit_(v, t):
        s.set(5, t, t + q, 0); s.set(6, t, t + 4 * q, v)
        s.set(5, t + q, t + 3 * q, 1); s.set(5, t + 3 * q, t + 4 * q, 0)
        return t + 4 * q
    for byte in ((0x3C << 1) | 0, 0x00, 0xAF):
        for i in range(7, -1, -1):
            t = bit_((byte >> i) & 1, t)
        t = bit_(0, t)                                   # ACK
    s.set(6, t, t + q, 0); s.set(5, t, t + q, 0); t += q  # подготовка STOP
    s.set(6, t, t + q, 0); t += q                         # SCL=1, SDA=0
    # ch7: маркер — импульс 37.5 мкс на 15 мс
    s.set(7, 0, n, 0); s.set(7, RATE * 0.015, RATE * 0.015 + 150, 1)
    raw = os.path.join(D, "synth.bin")
    open(raw, "wb").write(s.buf)
    out = os.path.join(D, "synth.sr")
    subprocess.run(["sigrok-cli", "-I", f"binary:numchannels=8:samplerate={RATE}", "-i", raw, "-o", out], check=True)
    return out


def can_bits(ident, data):
    """Расширенный кадр CAN до слота ACK: биты с бит-стаффингом и CRC-15 (как на линии)."""
    raw = [0] + [(ident >> (28 - i)) & 1 for i in range(11)] + [1, 1] + [(ident >> (17 - i)) & 1 for i in range(18)]
    raw += [0, 0, 0] + [(len(data) >> (3 - i)) & 1 for i in range(4)]
    for byte in data:
        raw += [(byte >> (7 - i)) & 1 for i in range(8)]
    crc = 0
    for bit in raw:
        top = ((crc >> 14) & 1) ^ bit
        crc = (crc << 1) & 0x7FFF
        crc ^= 0x4599 if top else 0
    raw += [(crc >> (14 - i)) & 1 for i in range(15)]
    out, run, last = [], 0, None
    for bit in raw:
        out.append(bit)
        run = run + 1 if bit == last else 1
        last = bit
        if run == 5:
            out.append(1 - bit)
            run, last = 1, 1 - bit
    return out + [1]                     # разделитель CRC; дальше слот ACK


def build_can():
    """D1 — линия, D0 — TX узла B: B подтверждает два кадра узла A и передаёт свой."""
    bitrate, n = 500_000, RATE // 100
    spb = RATE // bitrate
    s = Sig(n)
    t = spb * 20
    for ident, data, sender_is_b in ((0x1855F02A, b"\x2e\x65\x80", False), (0x1855F02A, b"\x11\x81", False),
                                     (0x140BAAE3, b"\x5c\x08\x80", True)):
        bits = can_bits(ident, data)
        for k, bit in enumerate(bits):
            s.set(1, t + k * spb, t + (k + 1) * spb, bit)
            if sender_is_b:
                s.set(0, t + k * spb, t + (k + 1) * spb, bit)
        ack = t + len(bits) * spb
        s.set(1, ack, ack + spb, 0)                       # слот ACK на линии: кадр принят
        if not sender_is_b:
            s.set(0, ack, ack + spb, 0)                   # подтверждает узел B
        t = ack + spb * 400        # пауза больше «кадра», который декодер строит из одиночного бита ACK
    raw = os.path.join(D, "can.bin")
    open(raw, "wb").write(s.buf)
    out = os.path.join(D, "can.sr")
    subprocess.run(["sigrok-cli", "-I", f"binary:numchannels=8:samplerate={RATE}", "-i", raw, "-o", out], check=True)
    return out, bitrate


def check_can():
    path, bitrate = build_can()
    c = Capture(path)
    frames = c.can_frames(1, bitrate)
    assert [(hex(f["id"]), f["data"], f["ack"], f["warnings"]) for f in frames] == [
        ("0x1855f02a", [0x2E, 0x65, 0x80], "dominant", []), ("0x1855f02a", [0x11, 0x81], "dominant", []),
        ("0x140baae3", [0x5C, 0x08, 0x80], "dominant", [])], frames
    assert frames[0]["t"] < frames[1]["t"] < frames[2]["t"]
    role = c.can_role(1, 0, bitrate)
    assert role["acked_by_other"] == 2 and role["other_own_frames"] == 1 and "TX узла" in role["verdict"], role
    reverse = c.can_role(0, 1, bitrate)
    assert "перепутаны" in reverse["verdict"], reverse
    report = c.channel_report(bitrate)          # каналы файлов из -I binary называются 0..7
    assert report[1]["longest_bits"] <= 5.01 and report[0]["single_bit"] > report[1]["single_bit"], report
    assert Capture.dronecan(0x180B63AA) == {"kind": "service", "priority": 24, "type": 11, "request": False,
                                            "dst": 99, "src": 42}
    assert Capture.dronecan(0x1855F02A) == {"kind": "message", "priority": 24, "dtid": 22000, "src": 42}
    print("CAN: линия, TX узла, ACK, DroneCAN — OK")


def approx(a, b, tol):
    return a is not None and abs(a - b) <= tol


def main():
    assert parse_rate("8m") == 8e6 and parse_rate("500k") == 5e5 and parse_rate("1.5MHz") == 1.5e6
    c = Capture(build())
    print(c.summary())
    assert c.samplerate == RATE and c.n == RATE // 50
    assert c.uart(0, 115200) == b"OK\r\n", c.uart(0, 115200)
    assert approx(c.frequency(1), 2000, 1), c.frequency(1)
    assert approx(c.duty(1), 0.25, 0.002), c.duty(1)
    st = c.pulse_stats(1)
    assert approx(st["mean"], 125e-6, 1e-6), st
    spi = c.spi(clk=2, mosi=3, cs=4)
    assert spi == [(0xA5, None), (0x3C, None)], spi
    ev = c.i2c(scl=5, sda=6)
    print("I2C:", ev)
    assert ev[0] == "Start" and ev[-1] == "Stop" and "Address write: 3C" in ev, ev
    tr = c.i2c_transactions(5, 6)
    assert tr == [{"addr": 0x3C, "read": False, "data": [0x00, 0xAF], "nack": False}], tr
    assert approx(c.first_edge(7, "r"), 0.015, 1e-6)
    assert approx(c.pulse_stats(7)["mean"], 150 / RATE, 1e-9)
    assert approx(c.delay(7, 7, "r", "f"), 150 / RATE, 1e-9)
    lat = [c.delay(7, 7, "r", "f", after=x) for x in c.edge_times(7, "r")]
    assert lat and approx(lat[0], 150 / RATE, 1e-9), lat
    per = [c.delay(1, 1, "r", "f", after=x) for x in c.edge_times(1, "r")]
    assert all(approx(p, 125e-6, 1e-6) for p in per if p is not None), per[:3]
    csv = c.export("csv")
    assert os.path.getsize(csv) > 0
    print("синтетика: OK")
    check_can()

    la = LogicAnalyzer("1m", driver="demo")
    cap = la.capture(os.path.join(D, "demo.sr"), duration=0.01)
    assert cap.n >= 9900 and cap.samplerate == 1e6, (cap.n, cap.samplerate)
    # Some demo-driver versions stop early with a trigger. This must not pass
    # as a successful capture merely because the exit code is zero.
    triggered = os.path.join(D, "demo_t.sr")
    try:
        cap = la.capture(triggered, samples=5000, trigger={0: "r"}, pretrigger=20)
        assert cap.n == 5000
    except LAError as exc:
        assert not isinstance(exc, TriggerTimeout), exc
        cap = Capture(triggered)
        assert 0 < cap.n < 5000 and cap.samplerate == 1e6, (cap.n, cap.samplerate)
        print("demo trigger: incomplete capture correctly rejected")
    # demo заканчивает запись без данных, если триггер не сработал (D7 демо всегда 1)
    try:
        la.capture(os.path.join(D, "never.sr"), samples=1000, trigger={7: "f"})
        raise AssertionError("ожидался TriggerTimeout")
    except TriggerTimeout:
        pass
    # настоящий fx2lafw ждёт триггер вечно — имитируем зависший sigrok-cli
    fake = os.path.join(D, "fake-sigrok")
    open(fake, "w").write("#!/bin/sh\nexec sleep 60\n"); os.chmod(fake, 0o755)
    hang = LogicAnalyzer("1m", driver="demo", sigrok=fake)
    hang.arm(os.path.join(D, "hang.sr"), samples=1000, trigger={7: "f"}, settle=0.1)
    t0 = time.time()
    try:
        hang.collect(timeout=1.0)
        raise AssertionError("ожидался TriggerTimeout")
    except TriggerTimeout:
        assert time.time() - t0 < 4 and hang.proc is None
    try:
        LogicAnalyzer("7m")
        raise AssertionError("ожидалась ошибка частоты")
    except ValueError:
        pass
    print("demo: OK")
    doctor()                     # без железа часть проверок красная — это нормально
    print("ВСЁ OK")


if __name__ == "__main__":
    main()
