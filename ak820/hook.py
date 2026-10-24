"""Global key-press listener (WH_KEYBOARD_LL) for the reactive lighting effect.
Only the key's light index and the press time are kept; nothing is logged or stored."""
import ctypes
import threading
import time
from ctypes import wintypes

from .keyboard import KEYS

VK = {
    0x1B: "esc", **{0x70 + i: f"f{i + 1}" for i in range(12)}, 0x2E: "delete",
    0xC0: "grave", **{0x30 + i: str(i) for i in range(10)}, 0xBD: "minus", 0xBB: "equal",
    0x08: "backspace", 0x24: "home", 0x09: "tab", 0xDB: "lbracket", 0xDD: "rbracket", 0xDC: "backslash",
    0x21: "pgup", 0x14: "capslock", 0xBA: "semicolon", 0xDE: "quote", 0x0D: "enter", 0x22: "pgdn",
    0xA0: "lshift", 0xBC: "comma", 0xBE: "period", 0xBF: "slash", 0xA1: "rshift", 0x26: "up",
    0xA2: "lctrl", 0x5B: "win", 0xA4: "lalt", 0x20: "space", 0xA5: "ralt", 0xA3: "rctrl",
    0x25: "left", 0x28: "down", 0x27: "right",
    **{0x41 + i: chr(ord("a") + i) for i in range(26)},
}
VK_TO_LIGHT = {vk: KEYS[name] for vk, name in VK.items() if name in KEYS}

WH_KEYBOARD_LL, WM_KEYDOWN, WM_SYSKEYDOWN, WM_QUIT = 13, 0x0100, 0x0104, 0x0012


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32 = ctypes.windll.user32
user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD)
user32.SetWindowsHookExW.restype = ctypes.c_void_p


class KeyPresses:
    """Runs the hook in its own thread; presses is a list of (light index, monotonic time)."""

    def __init__(self):
        self.presses = []
        self.thread = None
        self.thread_id = None

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        if self.thread and self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.thread = self.thread_id = None

    def recent(self, window):
        now = time.monotonic()
        self.presses = [p for p in self.presses if now - p[1] < window]
        return list(self.presses)

    def _run(self):
        self.thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        @HOOKPROC
        def proc(code, wparam, lparam):
            if code == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                vk = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
                light = VK_TO_LIGHT.get(vk)
                if light:
                    self.presses.append((light, time.monotonic()))
            return user32.CallNextHookEx(None, code, wparam, lparam)

        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, None, 0)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)
