import os, time, fcntl, statistics, sys
path = sys.argv[1]
def run(label, sync):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    lat = []
    for _ in range(300):
        t = time.perf_counter()
        os.write(fd, b"x" * 1100)
        sync(fd)
        lat.append((time.perf_counter() - t) * 1000)
    os.close(fd); os.unlink(path)
    lat.sort()
    print(f"{label:14s} p50={lat[len(lat)//2]:.2f}ms p99={lat[int(len(lat)*.99)]:.2f}ms max={lat[-1]:.2f}ms")
run("write+fsync", os.fsync)
run("write+FULLFSYNC", lambda fd: fcntl.fcntl(fd, fcntl.F_FULLFSYNC))
run("write only", lambda fd: None)
