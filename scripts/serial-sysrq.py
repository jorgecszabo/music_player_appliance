#!/usr/bin/env python3
"""Send Magic SysRq keys to the PandaBoard over the serial console and capture
the output (Python stdlib only).

Usage:  scripts/serial-sysrq.py [keys]      default keys: "wl"
  w = blocked (D-state) tasks with stacks, l = backtrace of all CPUs,
  t = all tasks (very long), m = memory info, h = help
Close picocom first: only one program should read the port.
Output is printed and also saved to build/serial-sysrq.log.
"""
import os
import sys
import termios
import time

PORT = os.environ.get("PANDA_SERIAL", "/dev/ttyUSB0")
KEYS = sys.argv[1] if len(sys.argv) > 1 else "wl"
READ_SECONDS = 10
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "serial-sysrq.log")


def open_port(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    iflag = 0
    oflag = 0
    lflag = 0
    cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, termios.B115200, termios.B115200, cc])
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_for(fd, seconds, out):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            data = b""
        if data:
            text = data.decode("utf-8", "replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            out.write(text)
        else:
            time.sleep(0.05)


def main():
    fd = open_port(PORT)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w") as out:
        out.write(f"# sysrq keys {KEYS!r} on {PORT} at {time.ctime()}\n")
        read_for(fd, 1, out)            # drain anything pending
        for key in KEYS:
            out.write(f"\n# ---- sysrq {key} ----\n")
            termios.tcsendbreak(fd, 0)  # BREAK = SysRq on a serial console
            time.sleep(0.2)
            os.write(fd, key.encode())
            read_for(fd, READ_SECONDS, out)
    os.close(fd)
    print(f"\nsaved to {os.path.normpath(LOG)}")


if __name__ == "__main__":
    main()
