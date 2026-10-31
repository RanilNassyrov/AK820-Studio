"""Screen colors for the screen-color lighting effect.

The primary path is DXGI Desktop Duplication (dxcam): the graphics card hands over the desktop
in well under a millisecond and returns nothing while the picture hasn't changed. GDI is the
fallback (remote desktop, unusual drivers); reading the desktop through it costs ~60-80 ms on a
2560x1440 screen, so it is polled less often."""
import ctypes
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image

W, H = 64, 36
HALFTONE, SRCCOPY, CAPTUREBLT = 4, 0x00CC0020, 0x40000000

user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
user32.GetDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.StretchBlt.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD)
# handles are 64-bit: without argtypes ctypes passes them as 32-bit ints and overflows
user32.GetDC.argtypes = (wintypes.HWND,)
user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
gdi32.SetStretchBltMode.argtypes = (wintypes.HDC, ctypes.c_int)
gdi32.SetBrushOrgEx.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
gdi32.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT)
gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
gdi32.DeleteDC.argtypes = (wintypes.HDC,)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class ScreenSampler:
    def __init__(self):
        try:
            user32.SetProcessDPIAware()  # real pixel size, not the scaled one
        except OSError:
            pass
        self.screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.screen)
        self.bmp = gdi32.CreateCompatibleBitmap(self.screen, W, H)
        gdi32.SelectObject(self.mem, self.bmp)
        gdi32.SetStretchBltMode(self.mem, HALFTONE)
        gdi32.SetBrushOrgEx(self.mem, 0, 0, None)
        self.info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), W, -H, 1, 32, 0, 0, 0, 0, 0, 0)
        self.buf = (ctypes.c_ubyte * (W * H * 4))()

    def grab(self):
        """(H, W, 3) uint8 RGB array of the primary monitor, averaged down to 64x36."""
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        gdi32.StretchBlt(self.mem, 0, 0, W, H, self.screen, 0, 0, sw, sh, SRCCOPY | CAPTUREBLT)
        gdi32.GetDIBits(self.mem, self.bmp, 0, H, self.buf, ctypes.byref(self.info), 0)
        bgra = np.frombuffer(self.buf, dtype=np.uint8).reshape(H, W, 4)
        return bgra[..., 2::-1].copy()

    def close(self):
        gdi32.DeleteObject(self.bmp)
        gdi32.DeleteDC(self.mem)
        user32.ReleaseDC(None, self.screen)


class Capture:
    """Background reader of the primary monitor: keeps a small (~90x160) RGB picture for the
    screen-color effect and counts frames for the FPS meter.

    Windows reports how many frames piled up since the last read (AccumulatedFrames), so 25 reads
    a second count every frame up to the monitor's refresh rate. That is the rate the desktop
    updates: frames a game draws above the refresh rate are not seen, and neither is an exclusive
    fullscreen game that takes the screen away from desktop duplication.
    """

    INTERVAL = 0.04
    GDI_INTERVAL = 0.25
    TINY = (0, 0, 32, 32)  # the counter needs no pixels, so copy as few as dxcam allows

    def __init__(self):
        self.cam = self.gdi = None
        self.small = None
        self.backend = ""
        self.fps = None
        self.pixels = self.count = False
        self.thread = None

    def want(self, pixels, count):
        """Which results are needed; the reader thread runs only while something is."""
        self.pixels, self.count = pixels, count
        if (pixels or count) and not self.thread:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        if not count:
            self.fps = None

    def _open(self):
        try:
            import dxcam
            self.cam = dxcam.create(output_color="BGRA")
            self.backend = "DXGI"
        except Exception:
            self.gdi = ScreenSampler()
            self.backend = "GDI"

    def _run(self):
        if not self.cam and not self.gdi:
            self._open()
        frames, since, first = 0, time.monotonic(), True
        while self.pixels or self.count:
            if self.gdi:
                self.small = self.gdi.grab()
                time.sleep(self.GDI_INTERVAL)
                continue
            try:
                # a still screen delivers nothing, and on some drivers that includes the first frame
                frame = self.cam.grab(region=None if self.pixels else self.TINY)
                piled = self.cam._duplicator.accumulated_frames  # pinned dxcam==0.3.0
            except Exception:
                time.sleep(1)
                continue
            if first:
                first, piled = False, 0  # counted while nobody was reading
                since = time.monotonic()
            if frame is not None:
                frames += piled
                if self.pixels and frame.shape[1] > self.TINY[2]:
                    step = max(1, frame.shape[1] // 160)
                    self.small = frame[::step, ::step, 2::-1]
            if self.pixels and self.small is None:
                gdi = ScreenSampler()
                self.small = gdi.grab()
                gdi.close()
            elapsed = time.monotonic() - since
            if elapsed >= 1:
                self.fps = round(frames / elapsed) if self.count else None
                frames, since = 0, time.monotonic()
            time.sleep(self.INTERVAL)
        self.thread = None

    def latest(self):
        return self.small


def zones(small, cols, rows):
    """Average color of each of cols x rows screen regions, as a (rows, cols, 3) float array."""
    im = Image.fromarray(np.ascontiguousarray(small))
    return np.asarray(im.resize((cols, rows), Image.BOX), dtype=np.float32)
