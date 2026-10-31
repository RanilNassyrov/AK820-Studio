"""AK820 Pro running the open QMK firmware (jdlien/ak820-pro): text, clock and lighting over raw HID.

Commands are 32-byte reports on usage page 0xFF60, framed like VIA's custom values. Unlike the
stock screen nothing here is written to flash, so updates are instant and can repeat freely. The
firmware has two text rows of printable ASCII and expires them after ~3 minutes without a push.
"""
import colorsys
import threading
import time
import unicodedata
import zlib

import hid

VID, PIDS = 0x0C45, (0x8009, 0x800A)
RAW_PAGE, RAW_USAGE = 0xFF60, 0x61
SET_VALUE = 0x07
RTC_CHANNEL, RTC_SET_TIME = 0x10, 0x01
TEXT_CHANNEL, TEXT_CLEAR, TEXT_SET_LINE, TEXT_PLAYBACK = 0x12, 0x02, 0x03, 0x04
VIA_RGB_CHANNEL, VIA_BRIGHTNESS, VIA_EFFECT, VIA_COLOR = 0x03, 0x01, 0x02, 0x04
VIA_GET_KEYCODE, VIA_SET_KEYCODE = 0x04, 0x05
FLASH_CHANNEL = 0x11
FC_ERASE, FC_BEGIN, FC_DATA, FC_END, FC_CRC, FC_UNLOCK, FC_CRC_NEXT = 0x02, 0x03, 0x04, 0x05, 0x06, 0x08, 0x09
FS_OK, FS_BUSY, FS_MORE = 0x00, 0x01, 0x04
SECTOR, CHUNK = 4096, 27
ANIM_SLOT = 0x540000  # the firmware's animation; Fn+G plays it
FRAME_MS = 100  # the firmware's fixed playback speed, whatever the file says
MATRIX = 6, 15
KC_G, KC_TRANSPARENT, ANIM_TOG = 0x000A, 0x0001, 0x7E11  # QK_KB_0 + 17 in via.json
FN_LAYERS = 1, 3  # Windows and Mac sides
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

    def request(self, *payload):
        with self.lock:
            self.dev.write(bytes((0, *payload)).ljust(33, b"\0"))
            for _ in range(8):  # a late echo of an earlier send() must not pass for this reply
                reply = bytes(self.dev.read(32, 2000))
                if not reply or reply[:3] == bytes(payload[:3]) or reply[0] == 0xFF:
                    break
        if not reply:
            raise OSError("клавиатура не ответила")
        if reply[:3] != bytes(payload[:3]):
            raise OSError(f"неожиданный ответ клавиатуры: {reply[:4].hex()}")
        return reply

    def flash(self, cmd, *args):
        for _ in range(2000):  # the firmware answers BUSY instead of stalling its key scan
            reply = self.request(SET_VALUE, FLASH_CHANNEL, cmd, *args)
            if reply[3] != FS_BUSY:
                break
        if reply[3] not in (FS_OK, FS_MORE):
            raise OSError(f"ошибка записи во флеш: код {reply[3]}")
        return reply

    def write_flash(self, addr, blob, progress=lambda done, total: None):
        """Erase, write and verify. Only the animation slot needs the unlock; the firmware refuses
        the screen's fonts at any time."""
        total = len(blob)
        self.flash(FC_UNLOCK, 1)
        try:
            sectors = (total + SECTOR - 1) // SECTOR
            for n in range(sectors):
                a = addr + n * SECTOR
                self.flash(FC_ERASE, a >> 16 & 255, a >> 8 & 255, a & 255)
                progress(n + 1, sectors + total // CHUNK)
            self.flash(FC_BEGIN, addr >> 16 & 255, addr >> 8 & 255, addr & 255)
            for off in range(0, total, CHUNK):
                part = blob[off:off + CHUNK]
                self.flash(FC_DATA, len(part), *part)
                progress(sectors + off // CHUNK, sectors + total // CHUNK)
            self.flash(FC_END)
            reply = self.flash(FC_CRC, addr >> 16 & 255, addr >> 8 & 255, addr & 255,
                               total >> 16 & 255, total >> 8 & 255, total & 255)
            while reply[3] == FS_MORE:
                reply = self.flash(FC_CRC_NEXT)
            got = int.from_bytes(reply[4:8], "big")
            if got != zlib.crc32(blob):
                raise OSError(f"проверка не прошла: {got:#010x} вместо {zlib.crc32(blob):#010x}")
        finally:
            self.flash(FC_UNLOCK, 0)

    def bind_anim_key(self):
        """The firmware's animation toggle is not on any key: put it on Fn+G (Fn+Delete switches the
        screen off). Returns False when that key already does something, so a user's own binding
        is left alone."""
        keycode = lambda layer, row, col: int.from_bytes(self.request(VIA_GET_KEYCODE, layer, row, col)[4:6], "big")
        spot = next(((r, c) for r in range(MATRIX[0]) for c in range(MATRIX[1])
                     if keycode(0, r, c) == KC_G), None)
        if not spot:
            return False
        free = [layer for layer in FN_LAYERS if keycode(layer, *spot) in (KC_TRANSPARENT, 0, ANIM_TOG)]
        for layer in free:
            self.request(VIA_SET_KEYCODE, layer, *spot, ANIM_TOG >> 8, ANIM_TOG & 255)
        return len(free) == len(FN_LAYERS)

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
