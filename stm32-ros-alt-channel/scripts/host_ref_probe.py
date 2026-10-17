import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
def host():
    s.sendto(b"t", ("127.0.0.1", 50123)); return float(s.recv(64))
h0, r0, m0 = host(), time.time(), time.monotonic()
ph, pr, pm = h0, r0, m0
while pm - m0 < 120:
    time.sleep(0.05)
    t1 = time.monotonic(); h = host(); m = time.monotonic(); r = time.time()
    dh, dr, dm = h - ph, r - pr, m - pm
    if max(dh, dr, dm) > 0.3 or abs(dr - dm) > 0.02:
        print("t=%6.1f host %.3f real %.3f mono %.3f rtt %.3f | real-host %+.3f mono-host %+.3f"
              % (m - m0, dh, dr, dm, m - t1, (r - r0) - (h - h0), (m - m0) - (h - h0)), flush=True)
    ph, pr, pm = h, r, m
print("итог: host %.3f real %.3f mono %.3f" % (h - h0, r - r0, m - m0))
