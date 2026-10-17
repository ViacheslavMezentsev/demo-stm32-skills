import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", 50123))
while True:
    _, addr = s.recvfrom(64)
    s.sendto(repr(time.perf_counter()).encode(), addr)
