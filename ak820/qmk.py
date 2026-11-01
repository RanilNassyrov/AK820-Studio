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
VIA_RGB_CHANNEL, VIA_BRIGHTNESS, VIA_EFFECT, VIA_SPEED, VIA_COLOR = 0x03, 0x01, 0x02, 0x03, 0x04
VIA_GET_VALUE, VIA_SAVE = 0x08, 0x09
VIA_GET_KEYCODE, VIA_SET_KEYCODE = 0x04, 0x05
FLASH_CHANNEL = 0x11
FC_ERASE, FC_BEGIN, FC_DATA, FC_END, FC_CRC, FC_UNLOCK, FC_CRC_NEXT = 0x02, 0x03, 0x04, 0x05, 0x06, 0x08, 0x09
FS_OK, FS_BUSY, FS_REFUSED, FS_MORE = 0x00, 0x01, 0x02, 0x04
SECTOR, CHUNK = 4096, 27
ANIM_SLOT = 0x540000  # the firmware's animation; Fn+G plays it
FRAME_MS = 100  # the firmware's fixed playback speed, whatever the file says
MATRIX = 6, 15
KC_G, KC_TRANSPARENT, ANIM_TOG = 0x000A, 0x0001, 0x7E11  # QK_KB_0 + 17 in via.json
FN_LAYERS = 1, 3  # Windows and Mac sides
BASE_LAYERS = 0, 2
# Keys printed differently from the firmware author's own layout: (row, col) -> printed key, and what
# his layout puts there on the Windows and Mac sides. Anything else there is the user's own choice.
PRINTED_KEYS = {
    (0, 13): (0x004C, (0x006E,)),          # Del, F19 in his layout
    (2, 14): (0x004B, (0x004C,)),          # PgUp, Del
    (3, 14): (0x004E, (0x004D,)),          # PgDn, End
    (3, 0): (0x0039, (0x5221, 0x5223)),    # Caps Lock, a second Fn
}
HOST_COLORS_CHANNEL, EFFECT_HOST, HOST_PER_PACKET = 0x14, 10, 9  # our firmware patch
# each LED's place on the board, QMK's 0-224 by 0-64 grid (keyboard.json)
LEDS = [
    (7, 5), (24, 5), (38, 5), (52, 5), (66, 5), (84, 5), (98, 5), (112, 5), (126, 5), (144, 5),
    (158, 5), (172, 5), (186, 5), (203, 5), (7, 19), (21, 19), (35, 19), (49, 19), (63, 19),
    (77, 19), (91, 19), (105, 19), (119, 19), (133, 19), (147, 19), (161, 19), (175, 19), (196, 19),
    (224, 19), (10, 29), (28, 29), (42, 29), (56, 29), (70, 29), (84, 29), (98, 29), (112, 29),
    (126, 29), (140, 29), (154, 29), (168, 29), (182, 29), (200, 29), (224, 29), (12, 40), (32, 40),
    (46, 40), (60, 40), (74, 40), (88, 40), (102, 40), (116, 40), (130, 40), (144, 40), (158, 40),
    (172, 40), (194, 40), (224, 40), (16, 51), (38, 51), (52, 51), (66, 51), (80, 51), (94, 51),
    (108, 51), (122, 51), (136, 51), (150, 51), (164, 51), (184, 51), (206, 53), (9, 61), (26, 61),
    (44, 61), (96, 61), (147, 61), (161, 61), (175, 61), (189, 64), (203, 64), (217, 64),
]
EFFECT_SOLID = 1
# QMK numbers the effects enabled in the firmware (keyboard.json), then its custom ones
# the flag says whether the effect uses the chosen color: rain and drops pick random ones
EFFECTS = [(1, "Один цвет", True), (2, "Буквы и модификаторы", True), (3, "Перелив всех клавиш", False),
           (4, "Радуга слева направо", False), (5, "Радуга сверху вниз", False), (6, "Капли", False),
           (7, "Тепловая карта нажатий", False), (8, "Дождь", False), (9, "Дрейф", True)]
MODIFIER_LEDS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 27, 28, 29, 43, 44, 56, 57, 58, 69, 70, 71, 72, 73, 75, 76, 77, 78, 79, 80)  # keyboard.json flags: "Буквы и модификаторы" colors these apart
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


class AnimationPlaying(Exception):
    pass


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
        if reply[3] == FS_REFUSED:  # the firmware's player holds the flash bus while it runs
            raise AnimationPlaying("Выключите GIF клавишами Fn+G и загрузите снова")
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

    def keycode(self, layer, row, col):
        return int.from_bytes(self.request(VIA_GET_KEYCODE, layer, row, col)[4:6], "big")

    def set_keycode(self, layer, row, col, code):
        self.request(VIA_SET_KEYCODE, layer, row, col, code >> 8, code & 255)

    def match_printed_keys(self):
        """Puts the keys back to what's printed on them. A flash resets the layout, hence every connect.
        Returns how many keys changed."""
        changed = 0
        for layer in BASE_LAYERS:
            for (row, col), (printed, authors) in PRINTED_KEYS.items():
                if self.keycode(layer, row, col) in authors:
                    self.set_keycode(layer, row, col, printed)
                    changed += 1
        return changed

    def bind_anim_key(self):
        """The firmware's animation toggle is not on any key: put it on Fn+G (Fn+Delete switches the
        screen off). Returns False when that key already does something, so a user's own binding
        is left alone."""
        spot = next(((r, c) for r in range(MATRIX[0]) for c in range(MATRIX[1])
                     if self.keycode(0, r, c) == KC_G), None)
        if not spot:
            return False
        current = {layer: self.keycode(layer, *spot) for layer in FN_LAYERS}
        for layer, code in current.items():
            if code in (KC_TRANSPARENT, 0):
                self.set_keycode(layer, *spot, ANIM_TOG)
        return all(code in (KC_TRANSPARENT, 0, ANIM_TOG) for code in current.values())

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

    def effect(self, mode, rgb, brightness, speed):
        """A firmware effect; brightness and speed 0-255. Stays until save() or a power-off."""
        h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        self.color = None
        self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_EFFECT, mode)
        self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_BRIGHTNESS, brightness)
        self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_SPEED, speed)
        self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_COLOR, round(h * 255), round(s * 255))

    def save_lighting(self):
        self.send(VIA_SAVE, VIA_RGB_CHANNEL, 0)

    def current_effect(self):
        return self.request(VIA_GET_VALUE, VIA_RGB_CHANNEL, VIA_EFFECT)[3]

    def has_host_colors(self):
        """Only our firmware build knows channel 0x14; a stock QMK one refuses the report."""
        try:
            self.request(SET_VALUE, HOST_COLORS_CHANNEL, 0, 1, 0, 0, 0)
            return True
        except OSError:
            return False

    def host_mode(self, brightness):
        self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_EFFECT, EFFECT_HOST)
        self.send(SET_VALUE, VIA_RGB_CHANNEL, VIA_BRIGHTNESS, brightness)
        self.color = None

    def host_frame(self, colors):
        """One color per LED, in LEDS order. Not saved anywhere, so it can run nonstop."""
        for first in range(0, len(colors), HOST_PER_PACKET):
            part = colors[first:first + HOST_PER_PACKET]
            self.send(SET_VALUE, HOST_COLORS_CHANNEL, first, len(part), *(c for rgb in part for c in rgb))

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
