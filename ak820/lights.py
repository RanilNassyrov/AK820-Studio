"""Software lighting effects, sent as live frames (~20 per second). Live frames aren't written to
flash, and the keyboard falls back to its own built-in effect when they stop."""
import colorsys
import math
import threading
import time

import numpy as np

from .hook import KeyPresses
from .keyboard import key_grid

BANDS = 16
RATE_HINT = 48000

# light mode -> (title, description) for the UI
EFFECTS = {
    "visualizer": ("Эквалайзер", "Клавиши танцуют под звук компьютера"),
    "wave": ("Радужная волна", "Плавная радуга бежит по клавиатуре"),
    "aurora": ("Северное сияние", "Переливы двух ваших цветов"),
    "breathing": ("Дыхание", "Плавное затухание и вспышка одним цветом"),
    "reactive": ("Реакция на нажатия", "Нажатая клавиша вспыхивает, от неё расходится круг"),
    "hardware": ("Встроенный эффект", "Один из 20 эффектов самой клавиатуры"),
}


def hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


class Loopback:
    """Captures the default output device through WASAPI loopback; keeps the newest samples."""

    def __init__(self):
        self.buf = np.zeros(4096, dtype=np.float32)
        self.rate = RATE_HINT
        self.stream = self.pa = None
        self.fresh = 0.0
        self.device = ""

    def open(self):
        import pyaudiowpatch as pyaudio
        self.pa = pyaudio.PyAudio()
        dev = self.pa.get_default_wasapi_loopback()
        self.rate = int(dev["defaultSampleRate"])
        ch = dev["maxInputChannels"]

        def cb(data, frames, info, status):
            a = np.frombuffer(data, dtype=np.float32).reshape(-1, ch).mean(axis=1)
            self.buf = np.concatenate([self.buf, a])[-4096:]
            self.fresh = time.monotonic()
            return None, pyaudio.paContinue

        self.stream = self.pa.open(format=pyaudio.paFloat32, channels=ch, rate=self.rate, input=True,
                                   input_device_index=dev["index"], frames_per_buffer=1024, stream_callback=cb)
        self.device = dev["name"]

    def close(self):
        for f in (getattr(self.stream, "close", None), getattr(self.pa, "terminate", None)):
            if f:
                f()
        self.stream = self.pa = None

    def bands(self):
        """BANDS levels 0..1 on a log frequency scale, or None while nothing is playing."""
        if time.monotonic() - self.fresh > 0.5:  # WASAPI sends no data during silence
            return None
        a = self.buf * np.hanning(len(self.buf))
        spec = np.abs(np.fft.rfft(a))
        freqs = np.fft.rfftfreq(len(a), 1 / self.rate)
        edges = np.geomspace(40, 14000, BANDS + 1)
        out = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            sel = spec[(freqs >= lo) & (freqs < hi)]
            out.append(sel.max() if len(sel) else 0.0)
        out = np.log10(1 + np.array(out) * 3) / 1.6
        return np.clip(out, 0, 1) if out.max() > 0.04 else None


class Lights(threading.Thread):
    def __init__(self, get_kb, get_cfg, log=print):
        super().__init__(daemon=True)
        self.get_kb, self.get_cfg, self.log = get_kb, get_cfg, log
        self.grid = key_grid()
        self.flash_color, self.flash_until = None, 0.0
        self.levels = np.zeros(BANDS)
        self.audio = Loopback()
        self.keys = KeyPresses()
        self.audio_retry = 0.0
        self.sent, self.sent_at = None, 0.0

    def flash(self, rgb, seconds=1.6):
        self.flash_color, self.flash_until = rgb, time.monotonic() + seconds

    def run(self):
        while True:
            cfg = self.get_cfg()
            mode = cfg["light_mode"]
            self._audio(mode == "visualizer")
            if mode == "reactive":
                self.keys.start()
            else:
                self.keys.stop()
            frame = self.frame(cfg, mode)
            kb = self.get_kb()
            if frame is None or kb is None:
                time.sleep(0.05)
                continue
            scale = cfg["light_brightness"] / 100
            if scale < 1:
                frame = {i: tuple(int(c * scale) for c in rgb) for i, rgb in frame.items()}
            gentle = cfg.get("keyboard_load") == "gentle"
            now = time.monotonic()
            # gentle: ~10 frames/s instead of ~20, and an unchanged frame is only resent as a
            # keep-alive, since the keyboard drops back to its own effect when frames stop
            if gentle and (now - self.sent_at < 0.1 or (frame == self.sent and now - self.sent_at < 0.4)):
                time.sleep(0.02)
                continue
            try:
                kb.live(frame)
                self.sent, self.sent_at = frame, now
            except OSError:
                time.sleep(0.5)  # unplugged: the service reconnects

    def _audio(self, wanted):
        if not wanted:
            if self.audio.stream:
                self.audio.close()
            return
        if self.audio.stream is None and time.monotonic() > self.audio_retry:
            try:
                self.audio.open()
                self.log(f"эквалайзер слушает: {self.audio.device}")
            except Exception as e:
                self.log(f"эквалайзер: нет доступа к звуку ({e}), повтор через 30 с")
                self.audio.close()
                self.audio_retry = time.monotonic() + 30

    def frame(self, cfg, mode):
        now = time.monotonic()
        if now < self.flash_until:
            # three soft pulses in the app's color, brightest in the middle of the keyboard
            k = 0.5 - 0.5 * math.cos((self.flash_until - now) * math.tau / 0.53)
            return {i: tuple(int(c * (0.15 + 0.85 * k * (1 - 0.6 * abs(x - 0.5)))) for c in self.flash_color)
                    for i, (x, y) in self.grid.items()}
        speed = 0.4 + 0.3 * cfg["light_speed"]
        c1, c2 = hex_rgb(cfg["light_color"]), hex_rgb(cfg["light_color2"])
        if mode == "visualizer":
            return self.visualizer(cfg, now)
        if mode == "wave":
            return {i: _hsv((x * 0.9 + y * 0.2 - now * 0.15 * speed) % 1) for i, (x, y) in self.grid.items()}
        if mode == "aurora":
            out = {}
            for i, (x, y) in self.grid.items():
                t = 0.5 + 0.25 * math.sin(x * 5 + now * speed) + 0.25 * math.sin(y * 4 - x * 2 + now * speed * 0.7)
                out[i] = tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))
            return out
        if mode == "breathing":
            k = 0.08 + 0.92 * (0.5 - 0.5 * math.cos(now * speed * 1.2))
            return {i: tuple(int(c * k) for c in c1) for i in self.grid}
        if mode == "reactive":
            return self.reactive(cfg, now, c1, c2)
        return None

    def visualizer(self, cfg, now):
        if self.audio.stream is None:
            return None
        b = self.audio.bands()
        if b is None:
            if self.levels.max() < 0.02:
                return None
            b = np.zeros(BANDS)
        # fast attack, slow fall, like a VU meter
        self.levels = np.where(b > self.levels, b, self.levels * 0.82)
        out = {}
        for i, (x, y) in self.grid.items():
            level = self.levels[min(BANDS - 1, int(x * BANDS))]
            height = 1 - y  # 0 = bottom row, 1 = F-key row
            if height > level:
                out[i] = (0, 0, 0)
            elif cfg["visualizer_style"] == "fire":
                out[i] = (255, int(200 * (1 - height)), 0)
            else:
                out[i] = _hsv((x * 0.8 + now * 0.05) % 1, 0.4 + 0.6 * level)
        return out

    def reactive(self, cfg, now, base, ripple):
        presses = self.keys.recent(1.2)
        dim = tuple(c // 8 for c in base)
        out = {i: dim for i in self.grid}
        for light, t in presses:
            if light not in self.grid:
                continue
            age = now - t
            px, py = self.grid[light]
            radius = age * 1.4
            for i, (x, y) in self.grid.items():
                # keyboard is ~3x wider than tall, so stretch rows to make the ring round
                d = math.hypot(x - px, (y - py) * 0.35)
                if i == light:
                    k = max(0.0, 1 - age / 0.6)
                    color = base
                else:
                    k = max(0.0, 1 - abs(d - radius) / 0.06) * max(0.0, 1 - age / 1.2)
                    color = ripple
                if k > 0:
                    out[i] = tuple(max(o, int(c * k)) for o, c in zip(out[i], color))
        return out


def _hsv(h, v=1.0):
    return tuple(int(255 * c) for c in colorsys.hsv_to_rgb(h, 1, v))
