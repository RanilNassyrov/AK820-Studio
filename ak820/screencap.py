"""Screen colors for the screen-color lighting effect.

The primary path is DXGI Desktop Duplication (dxcam): the graphics card hands over the desktop
in well under a millisecond and returns nothing while the picture hasn't changed. GDI is the
fallback (remote desktop, unusual drivers); reading the desktop through it costs ~60-80 ms on a
2560x1440 screen, so it is polled less often."""
import ctypes
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
    """Keeps the latest small (~90x160) RGB picture of the primary monitor."""

    GDI_INTERVAL = 0.25

    def __init__(self):
        self.cam = self.gdi = None
        self.small = None
        self.backend = ""
        self.gdi_at = 0.0

    def latest(self):
        if self.cam is None and self.gdi is None:
            try:
                import dxcam
                self.cam = dxcam.create(output_color="BGRA")
                self.backend = "DXGI"
            except Exception:
                self.gdi = ScreenSampler()
                self.backend = "GDI"
        if self.cam is not None:
            try:
                frame = self.cam.grab()  # None while the screen hasn't changed
            except Exception:
                frame = None
            if frame is not None:
                step = max(1, frame.shape[1] // 160)
                self.small = frame[::step, ::step, 2::-1]
            elif self.small is None:
                # duplication only delivers a frame once something on screen changes, so a still
                # desktop needs one slow GDI shot to start from
                gdi = ScreenSampler()
                self.small = gdi.grab()
                gdi.close()
        elif time.monotonic() - self.gdi_at >= self.GDI_INTERVAL:
            self.small = self.gdi.grab()
            self.gdi_at = time.monotonic()
        return self.small

    def close(self):
        if self.cam is not None:
            try:
                self.cam.release()
            except Exception:
                pass
        if self.gdi is not None:
            self.gdi.close()
        self.cam = self.gdi = self.small = None


def zones(small, cols, rows):
    """Average color of each of cols x rows screen regions, as a (rows, cols, 3) float array."""
    im = Image.fromarray(np.ascontiguousarray(small))
    return np.asarray(im.resize((cols, rows), Image.BOX), dtype=np.float32)
