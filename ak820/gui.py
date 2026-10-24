"""AK820 Studio window (pywebview + HTML) with a tray icon. Closing the window hides it to the tray;
the service keeps running until "Выход" in the tray menu."""
import ctypes
import subprocess
import sys
import threading
from pathlib import Path

import pystray
import webview
from PIL import Image, ImageDraw

from . import render
from .keyboard import KEYS, MODES, ROW_ENDS
from .lights import EFFECTS
from .service import ROOT, UPLOADING, Service

UI = Path(__file__).resolve().parent / "ui" / "index.html"
STARTUP = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/AK820 Studio.lnk"

HW_NAMES = {
    "off": "Выключено", "static": "Статичный", "singleon": "Зажигание при нажатии",
    "singleoff": "Гашение при нажатии", "glittering": "Мерцание", "falling": "Звездопад",
    "colourful": "Разноцветный", "breath": "Дыхание", "spectrum": "Спектр", "outward": "Из центра",
    "scrolling": "Прокрутка", "rolling": "Перекат", "rotating": "Вращение", "explode": "Взрыв",
    "launch": "Запуск", "ripples": "Рябь", "flowing": "Поток", "pulsating": "Пульсация",
    "tilt": "Наклон", "shuttle": "Челнок",
}
LABELS = {
    "esc": "Esc", "delete": "Del", "grave": "`", "minus": "-", "equal": "=", "backspace": "⌫",
    "home": "Home", "tab": "Tab", "lbracket": "[", "rbracket": "]", "backslash": "\\", "pgup": "PgUp",
    "capslock": "Caps", "semicolon": ";", "quote": "'", "enter": "Enter", "pgdn": "PgDn",
    "lshift": "Shift", "comma": ",", "period": ".", "slash": "/", "rshift": "Shift", "up": "↑",
    "lctrl": "Ctrl", "win": "Win", "lalt": "Alt", "space": "", "ralt": "Alt", "fn": "Fn",
    "rctrl": "Ctrl", "left": "←", "down": "↓", "right": "→",
}
WIDTHS = {"backspace": 2, "tab": 1.5, "backslash": 1.5, "capslock": 1.75, "enter": 2.25,
          "lshift": 2.25, "rshift": 1.75, "lctrl": 1.25, "win": 1.25, "lalt": 1.25, "space": 6.25}
APPS = [  # (match, title) shown in the notifications page
    ("outlook", "Outlook"), ("mail", "Почта Windows"), ("почта", "Почта"), ("gmail", "Gmail"),
    ("teams", "Microsoft Teams"), ("discord", "Discord"), ("telegram", "Telegram"),
    ("whatsapp", "WhatsApp"), ("slack", "Slack"),
]


def layout():
    rows, row = [], []
    for name, idx in KEYS.items():
        row.append({"name": name, "idx": idx, "label": LABELS.get(name, name.upper()), "w": WIDTHS.get(name, 1)})
        if name in ROW_ENDS:
            rows.append(row)
            row = []
    return rows


class Api:
    def __init__(self, service):
        self._s = service
        self._window = None

    def init(self):
        return {
            "cfg": self._s.cfg,
            "effects": [{"id": k, "title": t, "desc": d} for k, (t, d) in EFFECTS.items()],
            "hw_modes": [{"id": i, "name": HW_NAMES[n], "flags": f} for i, (n, f) in MODES.items()],
            "idle": [{"id": k, "title": t} for k, t in render.IDLE.items()],
            "layout": layout(),
            "apps": [{"match": m, "title": t, "color": "#%02x%02x%02x" % render.app_style(m)[1],
                      "label": render.app_style(m)[0]} for m, t in APPS],
            "autostart": STARTUP.exists(),
        }

    def status(self):
        return self._s.status()

    def update(self, changes):
        self._s.update(changes)
        return self._s.cfg

    def refresh_screen(self):
        self._s.refresh_screen()

    def test_notice(self, app):
        self._s.test_notice(app)

    def sync_time(self):
        self._s.sync_time()

    def apply_hardware(self):
        self._s.apply_hardware()

    def apply_keys(self, colors):
        self._s.apply_keys(colors)

    def choose_image(self):
        res = self._window.create_file_dialog(webview.OPEN_DIALOG, file_types=("Картинки (*.gif;*.png;*.jpg;*.jpeg;*.webp;*.bmp)",))
        if not res:
            return None
        self._s.update({"idle_style": "custom", "idle_custom": res[0]})
        return res[0]

    def set_autostart(self, on):
        if on:
            target = Path(sys.executable).with_name("pythonw.exe")
            ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{STARTUP}');"
                  f"$s.TargetPath='{target}';$s.Arguments='-m ak820 gui --minimized';"
                  f"$s.WorkingDirectory='{ROOT}';$s.IconLocation='{UI.with_name('icon.ico')}';$s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000, check=False)
        elif STARTUP.exists():
            STARTUP.unlink()
        return STARTUP.exists()

    def hide(self):
        self._window.hide()


def tray_image():
    im = Image.new("RGBA", (64, 64))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((2, 14, 62, 54), 9, fill=(124, 92, 255))
    for r in range(3):
        for c in range(6 - (r == 2) * 2):
            x = 9 + c * 8 + (r == 2) * 8
            d.rounded_rectangle((x, 21 + r * 10, x + 5, 26 + r * 10), 1, fill=(255, 255, 255))
    d.rounded_rectangle((44, 3, 60, 13), 2, fill=(0, 224, 255))
    return im


def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AK820StudioApp")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "AK820 Studio уже запущена — значок в трее, рядом с часами.",
                                         "AK820 Studio", 0x40)
        sys.exit(0)


def run(minimized=False):
    single_instance()
    service = Service()
    service.start()
    api = Api(service)
    window = webview.create_window("AK820 Studio", str(UI), js_api=api, width=1180, height=780,
                                   min_size=(980, 640), background_color="#0d0e14",
                                   hidden=minimized or service.cfg["start_minimized"])
    api._window = window
    quitting = threading.Event()

    def on_closing():
        if quitting.is_set():
            return True
        window.hide()
        return False

    window.events.closing += on_closing

    def show(icon=None, item=None):
        window.show()
        window.restore()

    def quit_app(icon, item):
        quitting.set()
        icon.stop()
        with UPLOADING:  # never cut an upload short: the screen would freeze on "Loading"
            window.destroy()

    icon = pystray.Icon("AK820 Studio", tray_image(), "AK820 Studio", pystray.Menu(
        pystray.MenuItem("Открыть", show, default=True),
        pystray.MenuItem("Выход", quit_app)))
    icon.run_detached()
    webview.start(gui="edgechromium")
