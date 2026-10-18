"""Compare WSL clocks to Windows host_clock.py without changing either clock."""
import argparse
import json
import math
import socket
import time
from wsl_tick import read_timex


def summarize(first, last, steps, max_rtt):
    elapsed = {key: last[key] - first[key] for key in ("host", "real", "mono", "raw")}
    if elapsed["host"] <= 0:
        raise ValueError("часы хоста не продвинулись")
    return dict(elapsed_seconds=elapsed,
                ratios_to_host={key: elapsed[key] / elapsed["host"] for key in ("real", "mono", "raw")},
                realtime_steps=steps, max_rtt_raw_seconds=max_rtt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=120)
    parser.add_argument("--port", type=int, default=50123)
    parser.add_argument("--out", help="JSON report")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds должен быть положительным конечным числом")
    raw = lambda: time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.connect(("127.0.0.1", args.port))
        def sample():
            start = raw()
            sock.send(b"t")
            host = float(sock.recv(64))
            end = raw()
            if not math.isfinite(host):
                raise ValueError("некорректные часы хоста")
            return dict(host=host, real=time.time(), mono=time.monotonic(), raw=end, rtt=end-start)
        before = read_timex()
        first = previous = sample()
        steps = 0
        worst_rtt = first["rtt"]
        while raw() - first["raw"] < args.seconds:
            time.sleep(0.05)
            current = sample()
            # A realtime jump relative to monotonic is distinct from transport latency.
            if abs((current["real"] - previous["real"]) - (current["mono"] - previous["mono"])) > 0.02:
                steps += 1
            worst_rtt = max(worst_rtt, current["rtt"])
            previous = current
    report = summarize(first, previous, steps, worst_rtt)
    report.update(timex_before=before, timex_after=read_timex())
    text = json.dumps(report, indent=2)
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)

if __name__ == "__main__":
    main()
