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


def fullscreen_state():
    """Windows' state for the foreground app; 2, 3, 4 and 7 mean something fullscreen covers the
    screen (busy, Direct3D exclusive, presentation, fullscreen app)."""
    state = ctypes.c_int()
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)):
        return 0
    return state.value


def boost_thread():
    """A game keeps every core busy: at normal priority a lighting frame took 186-395 ms instead of
    60. These threads sleep nearly all the time, so raising them costs the game almost nothing."""
    k = ctypes.windll.kernel32
    k.GetCurrentThread.restype = ctypes.c_void_p
    k.SetThreadPriority.argtypes = (ctypes.c_void_p, ctypes.c_int)
    k.SetThreadPriority(k.GetCurrentThread(), 2)  # THREAD_PRIORITY_HIGHEST


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
    screen-color effect."""

    INTERVAL = 0.04
    GDI_INTERVAL = 0.25
    STALE = 2  # seconds without a frame before asking GDI, in case duplication has stopped working

    MAX_ERRORS = 25  # about a second of failed reads before reopening the capture

    def __init__(self, log=print):
        self.log = log
        self.cam = self.gdi = None
        self.small = None
        self.exclusive = False  # a game in exclusive fullscreen: only black can be read
        self.backend = ""
        self.running = False
        self.thread = None
        self.last_frame = 0.0
        self.stats = {"frames": 0, "errors": 0, "gdi": 0}

    def want(self, running):
        """The reader thread runs only while the effect needs it."""
        self.running = running
        if running and not self.thread:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _open(self):
        try:
            import dxcam
            self.cam = dxcam.create(output_color="BGRA")
            self.backend = "DXGI"
        except Exception:
            self.gdi = ScreenSampler()
            self.backend = "GDI"

    def _close_cam(self):
        if self.cam:
            try:
                self.cam.release()
            except Exception:
                pass
        self.cam = None

    def _run(self):
        boost_thread()
        errors = 0
        while self.running:
            exclusive = fullscreen_state() == 3
            if exclusive != self.exclusive:
                self.exclusive = exclusive
                self.log("игра в эксклюзивном полноэкранном режиме: экран недоступен, запасной эффект"
                         if exclusive else "экран снова доступен")
            if exclusive:
                # the game owns the display, reads give black or errors, so don't touch it
                self._close_cam()
                time.sleep(0.25)
                continue
            if not self.cam and not self.gdi:
                self._open()
            if self.gdi:
                self.small = self.gdi.grab()
                time.sleep(self.GDI_INTERVAL)
                continue
            try:
                frame = self.cam.grab()
                errors = 0
            except Exception as e:
                frame = None
                errors += 1
                self.stats["errors"] += 1
                if errors >= self.MAX_ERRORS:  # duplication died: only a new one helps
                    self.log(f"захват экрана перезапущен: {type(e).__name__}: {e}")
                    self._close_cam()
                    errors = 0
                    time.sleep(1)
                    continue
            now = time.monotonic()
            if frame is not None:
                step = max(1, frame.shape[1] // 160)
                self.small = frame[::step, ::step, 2::-1]
                self.last_frame = now
                self.stats["frames"] += 1
            elif now - self.last_frame >= self.STALE:
                # a still screen delivers nothing, but so does a game that took the screen from
                # desktop duplication: without this check the keys would stay on the last colors
                gdi = ScreenSampler()
                self.small = gdi.grab()
                gdi.close()
                self.last_frame = now
                self.stats["gdi"] += 1
            time.sleep(self.INTERVAL)
        self.exclusive = False
        self.thread = None

    def latest(self):
        return self.small


def zones(small, cols, rows):
    """Average color of each of cols x rows screen regions, as a (rows, cols, 3) float array."""
    im = Image.fromarray(np.ascontiguousarray(small))
    return np.asarray(im.resize((cols, rows), Image.BOX), dtype=np.float32)
