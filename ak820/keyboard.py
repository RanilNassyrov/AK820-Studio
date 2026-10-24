"""AJAZZ AK820 Pro: screen, clock and live RGB over USB HID on Windows.

Protocol ported from hcristosm/ak820-linux (MIT, (c) 2026 hcristosm), which recovered it
from the vendor's DeviceDriver.exe:
  - commands: 64-byte feature reports, vendor collection with usage page 0xFF13
  - screen data: 4096-byte output reports, vendor collection with usage page 0xFF68
Works only over the USB cable: Bluetooth and the 2.4 GHz dongle don't expose these collections.
"""
import threading
import time

import hid

VID = 0x0C45
PIDS = (0x8009, 0x800A)
DELAY = 0.035  # vendor <cmd_delaytime value="35"/>
W = H = 128
HEAD, MAX_FRAMES, CHUNK = 256, 140, 4096

# key name -> light index, from the vendor's layouts/rgb-keyboard.xml
KEYS = dict(
    esc=1, f1=2, f2=3, f3=4, f4=5, f5=6, f6=7, f7=8, f8=9, f9=10, f10=11, f11=12, f12=13, delete=119,
    grave=19, **{"1": 20, "2": 21, "3": 22, "4": 23, "5": 24, "6": 25, "7": 26, "8": 27, "9": 28, "0": 29},
    minus=30, equal=31, backspace=103, home=117,
    tab=37, q=38, w=39, e=40, r=41, t=42, y=43, u=44, i=45, o=46, p=47, lbracket=48, rbracket=49,
    backslash=67, pgup=118,
    capslock=55, a=56, s=57, d=58, f=59, g=60, h=61, j=62, k=63, l=64, semicolon=65, quote=66,
    enter=85, pgdn=121,
    lshift=73, z=74, x=75, c=76, v=77, b=78, n=79, m=80, comma=81, period=82, slash=83, rshift=84, up=101,
    lctrl=91, win=92, lalt=93, space=94, ralt=95, fn=96, rctrl=98, left=99, down=100, right=102,
)
ROW_ENDS = {"delete", "home", "pgup", "pgdn", "up", "right"}  # last key of each row, in KEYS order

# Built-in effects: id -> (name, flags). Flags from the vendor's table: 1 brightness, 2 speed,
# 4 horizontal direction, 8 vertical direction, 0x10 color, 0x20 rainbow.
MODES = {
    0: ("off", 0x00), 1: ("static", 0x31), 2: ("singleon", 0x33), 3: ("singleoff", 0x33),
    4: ("glittering", 0x33), 5: ("falling", 0x33), 6: ("colourful", 0x03), 7: ("breath", 0x33),
    8: ("spectrum", 0x03), 9: ("outward", 0x33), 10: ("scrolling", 0x3B), 11: ("rolling", 0x37),
    12: ("rotating", 0x37), 13: ("explode", 0x33), 14: ("launch", 0x33), 15: ("ripples", 0x33),
    16: ("flowing", 0x37), 17: ("pulsating", 0x33), 18: ("tilt", 0x37), 19: ("shuttle", 0x33),
}


def key_grid():
    """light index -> (x, y), both 0..1: rank within the row and row number (top to bottom)."""
    rows, row = [], []
    for name, idx in KEYS.items():
        row.append(idx)
        if name in ROW_ENDS:
            rows.append(row)
            row = []
    return {idx: (i / (len(r) - 1), j / (len(rows) - 1)) for j, r in enumerate(rows) for i, idx in enumerate(r)}


class NotConnected(Exception):
    pass


def _find(usage_page):
    for d in hid.enumerate(VID):
        if d["product_id"] in PIDS and d["usage_page"] == usage_page:
            return d["path"]
    bt = any(d["product_id"] == 0xFEFE for d in hid.enumerate(VID))
    raise NotConnected("AK820 Pro is on Bluetooth: plug in the USB cable" if bt
                       else "AK820 Pro not found: plug it in with the USB cable")


class Keyboard:
    def __init__(self):
        self.ctl = hid.device()
        self.ctl.open_path(_find(0xFF13))
        self.lcd = None
        self.lock = threading.Lock()  # screen uploads and live RGB frames must not interleave

    def close(self):
        for d in (self.ctl, self.lcd):
            if d:
                d.close()

    def feature(self, data, readback=True, delay=DELAY):
        """Vendor send routine: sleep, SetFeature(64 bytes), sleep, GetFeature."""
        for off in range(0, len(data), 64):
            time.sleep(delay)
            self.ctl.send_feature_report(b"\0" + data[off:off + 64].ljust(64, b"\0"))
            if readback:
                time.sleep(delay)
                self.ctl.get_feature_report(0, 65)

    def cmd(self, op, count=0, extra=b"", readback=True, delay=DELAY):
        """0x04 <op> header; bytes 8-9 = number of data packets that follow."""
        p = bytearray(64)
        p[0], p[1] = 0x04, op
        p[2:2 + len(extra)] = extra
        p[8], p[9] = count & 0xFF, count >> 8
        self.feature(bytes(p), readback, delay)

    def upload_screen(self, blob, progress=lambda i, n: None):
        """blob from screen_blob(). Written to the keyboard's flash, so don't call it in a loop."""
        if self.lcd is None:
            self.lcd = hid.device()
            self.lcd.open_path(_find(0xFF68))
        n = len(blob) // CHUNK
        with self.lock:
            self.cmd(0x18)
            self.cmd(0x72, n, extra=b"\x01")  # byte 2 = screen slot
            for i in range(n):
                self.lcd.write(b"\0" + blob[i * CHUNK:(i + 1) * CHUNK])
                self.lcd.read(64, 300)  # vendor waits up to 300 ms for an ack
                progress(i + 1, n)
            self.cmd(0x02)

    def set_mode(self, mode, rgb=None, brightness=5, speed=3, direction=0):
        """Built-in effect, saved on the keyboard. rgb=None means rainbow. brightness, speed 0..5."""
        with self.lock:
            self.cmd(0x18)
            self.cmd(0x13, 1)
            self.feature(light_packet(mode, rgb or (255, 0, 0), rgb is None, brightness, speed, direction),
                         readback=False)
            self.cmd(0x02)
            self.cmd(0xF0, readback=False)

    def set_keys(self, colors, brightness=5):
        """Per-key colors {light index: (r, g, b)}, saved as the keyboard's custom mode."""
        p = bytearray(64)
        p[0], p[9] = 0x80, brightness
        p[14:16] = b"\xaa\x55"
        with self.lock:
            for op, count, data in ((0x13, 1, bytes(p)), (0x23, 9, keys_packet(colors))):
                self.cmd(0x18)
                self.cmd(op, count)
                self.feature(data, readback=False)
                self.cmd(0x02)
                self.cmd(0xF0, readback=False)

    def sync_time(self):
        with self.lock:
            self.cmd(0x18)
            self.cmd(0x28, 1)
            self.feature(time_packet(time.localtime()))
            self.cmd(0x02)

    def live(self, colors):
        """One real-time RGB frame {light index: (r, g, b)}. Not saved: the keyboard returns to its
        own effect shortly after frames stop."""
        with self.lock:
            self.cmd(0x20, 8, delay=0.005)
            self.feature(live_packet(colors), readback=False, delay=0.002)
            self.cmd(0x02, delay=0.005)


def time_packet(t, slot=1):
    p = bytearray(64)
    p[1], p[2] = slot, 0x5A
    p[3:9] = bytes([t.tm_year % 2000, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec])
    p[10] = (t.tm_wday + 1) % 7  # Sunday = 0
    p[62:64] = b"\xaa\x55"
    return bytes(p)


def light_packet(mode, rgb, rainbow, brightness, speed, direction):
    p = bytearray(64)
    p[0] = mode
    if mode:
        p[1:4] = bytes(rgb)
        p[8], p[9], p[10], p[11] = int(rainbow), brightness, speed, direction
    p[14:16] = b"\xaa\x55"
    return bytes(p)


def keys_packet(colors):
    """576 bytes: 144 slots of (index, r, g, b), ending in aa 55."""
    p = bytearray(0x240)
    for i, rgb in colors.items():
        p[i * 4:i * 4 + 4] = bytes([i, *rgb])
    p[0x23E:0x240] = b"\xaa\x55"
    return bytes(p)


def live_packet(colors):
    """512 bytes of consecutive (index, r, g, b) entries, at most 128."""
    return b"".join(bytes([i, *rgb]) for i, rgb in sorted(colors.items())[:128]).ljust(512, b"\0")


def rgb565(img):
    """Pillow RGB image (W x H) -> little-endian RGB565 bytes."""
    import numpy as np
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    v = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) | (a[..., 2] >> 3)
    return v.astype("<u2").tobytes()


def screen_blob(frames):
    """frames: [(PIL image, delay_ms)] -> 256-byte header + RGB565 frames, padded with 0xFF to 4 KiB."""
    frames = frames[:MAX_FRAMES]
    head = bytearray(b"\xff" * HEAD)
    head[0] = len(frames)
    for i, (_, ms) in enumerate(frames):
        head[1 + i] = min(255, max(1, ms // 2))  # unit is 2 ms
    blob = head + b"".join(rgb565(im) for im, _ in frames)
    return bytes(blob + b"\xff" * (-len(blob) % CHUNK))
