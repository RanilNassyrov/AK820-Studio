"""AK820 Pro running the open QMK firmware (jdlien/ak820-pro): text, clock and lighting over raw HID.

Commands are 32-byte reports on usage page 0xFF60, framed like VIA's custom values. Unlike the
stock screen nothing here is written to flash, so updates are instant and can repeat freely. The
firmware has two text rows of printable ASCII and expires them after ~3 minutes without a push.
"""
import colorsys
import threading
import time
import unicodedata

import hid

VID, PIDS = 0x0C45, (0x8009, 0x800A)
RAW_PAGE, RAW_USAGE = 0xFF60, 0x61
SET_VALUE = 0x07
RTC_CHANNEL, RTC_SET_TIME = 0x10, 0x01
TEXT_CHANNEL, TEXT_CLEAR, TEXT_SET_LINE, TEXT_PLAYBACK = 0x12, 0x02, 0x03, 0x04
VIA_RGB_CHANNEL, VIA_BRIGHTNESS, VIA_EFFECT, VIA_COLOR = 0x03, 0x01, 0x02, 0x04
EFFECT_SOLID = 1
ICON_NONE, ICON_PLAY, ICON_PAUSE, ICON_STOP = 0, 1, 2, 3
LINE_MAX = (19, 21)  # line 0 shares its row with the play icon

PUNCTUATION = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "«": '"', "»": '"'}
CYRILLIC = dict(zip("абвгдезийклмнопрстуфхцыэ", "abvgdezijklmnoprstufhcye"))
CYRILLIC.update({"ё": "yo", "ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ь": "", "ю": "yu", "я": "ya"})


def to_ascii(text):
    """The firmware's fonts are printable ASCII, so Cyrillic is transliterated and other scripts
    (Hangul, kanji) are dropped: Korean titles keep their Latin part."""
    out = []
    for ch in text or "":
        ch = PUNCTUATION.get(ch, ch)
        latin = CYRILLIC.get(ch.lower())
        if latin is not None:
            ch = latin.capitalize() if ch.isupper() else latin
        out.append(ch)
    text = unicodedata.normalize("NFKD", "".join(out)).encode("ascii", "ignore").decode()
    return " ".join(text.replace("()", " ").split())


def find():
    for d in hid.enumerate(VID):
        if d["product_id"] in PIDS and d["usage_page"] == RAW_PAGE and d["usage"] == RAW_USAGE:
            return d["path"]
    return None


class QmkKeyboard:
    qmk = True

    def __init__(self, path=None):
        self.dev = hid.device()
        self.dev.open_path(path or find())
        self.lock = threading.Lock()
        self.color = None

    def close(self):
        self.dev.close()

    def send(self, *payload):
        """One report. The firmware echoes each one, and unread echoes would fill the queue."""
        with self.lock:
            self.dev.write(bytes((0, *payload)).ljust(33, b"\0"))
            self.dev.read(32, 100)

    def sync_time(self):
        t = time.localtime()
        self.send(SET_VALUE, RTC_CHANNEL, RTC_SET_TIME, t.tm_year % 100, t.tm_mon, t.tm_mday,
                  (t.tm_wday + 1) % 7, t.tm_hour, t.tm_min, t.tm_sec)

    def text(self, line0, line1="", icon=ICON_NONE):
        for n, s in enumerate((line0, line1)):
            data = to_ascii(s)[:LINE_MAX[n]].encode("ascii")
            self.send(SET_VALUE, TEXT_CHANNEL, TEXT_SET_LINE, n, icon, *data)

    def clear_text(self):
        self.send(SET_VALUE, TEXT_CHANNEL, TEXT_CLEAR)

    def playback(self, state, position=0, duration=0):
        """State 0 hands the band back to the clock; times are whole seconds."""
        self.send(SET_VALUE, TEXT_CHANNEL, TEXT_PLAYBACK, state, position >> 8 & 255, position & 255,
                  duration >> 8 & 255, duration & 255)

    def solid(self, rgb):
        """The whole keyboard in one color, through VIA's lighting channel (not saved)."""
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        hsv = round(h * 255), round(s * 255), round(v * 255)
        if hsv == self.color:
            return
        if self.color is None:
            self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_EFFECT, EFFECT_SOLID)
        self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_BRIGHTNESS, hsv[2])
        self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_COLOR, hsv[0], hsv[1])
        self.color = hsv
