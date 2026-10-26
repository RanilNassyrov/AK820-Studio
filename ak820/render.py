"""128x128 animations for the keyboard screen. Every function returns [(PIL image, delay_ms)] that loops."""
import io
import math
import random
import re

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

S = 128
_fonts = {}


def font(size, bold=False, mono=False):
    name = "consolab.ttf" if mono else "segoeuib.ttf" if bold else "segoeui.ttf"
    if (name, size) not in _fonts:
        _fonts[name, size] = ImageFont.truetype(name, size)
    return _fonts[name, size]


def clean(text):
    """Segoe UI has no emoji: drop them instead of drawing boxes."""
    text = re.sub(r"[\U00010000-\U0010FFFF️‍]", "", text or "")
    return " ".join(text.split())


def fit(text, f, width):
    if f.getlength(text) <= width:
        return text
    while text and f.getlength(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap(text, f, width, max_lines):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if f.getlength(trial) <= width:
            line = trial
            continue
        if line:
            lines.append(line)
        line = word
        if len(lines) == max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines and " ".join(lines) != text:
        lines[-1] = fit(lines[-1] + "…", f, width)
    return [fit(l, f, width) for l in lines]


def rounded(im, radius):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius, fill=255)
    out = Image.new("RGBA", im.size)
    out.paste(im, (0, 0), mask)
    return out


def dominant(im):
    small = im.convert("RGB").resize((16, 16))
    r, g, b = [sum(c) / 256 for c in zip(*small.getdata())]
    # push toward a saturated version so the accent isn't muddy
    m = max(r, g, b, 1)
    return tuple(int(min(255, 60 + 195 * c / m)) for c in (r, g, b))


# ---------------------------------------------------------------- now playing

def now_playing(track, frames_n=1):
    cover = None
    if track.cover:
        try:
            cover = Image.open(io.BytesIO(track.cover)).convert("RGB")
        except OSError:
            cover = None
    accent = dominant(cover) if cover else (120, 90, 255)

    if cover:
        bg = ImageOps.fit(cover, (S, S)).filter(ImageFilter.GaussianBlur(10))
        bg = Image.blend(bg, Image.new("RGB", (S, S)), 0.6)
        art = rounded(ImageOps.fit(cover, (62, 62)), 8)
    else:
        bg = Image.linear_gradient("L").resize((S, S)).rotate(45)
        bg = ImageOps.colorize(bg, (15, 10, 40), (60, 20, 90))
        art = Image.new("RGBA", (62, 62))
        d = ImageDraw.Draw(art)
        d.rounded_rectangle((0, 0, 61, 61), 8, fill=(40, 30, 80))
        # eighth-note pair drawn by hand: Segoe UI has no music glyphs
        for x in (20, 40):
            d.ellipse((x - 7, 38, x + 1, 45), fill=accent)
            d.rectangle((x - 1, 18, x + 1, 42), fill=accent)
        d.polygon((19, 16, 41, 12, 41, 18, 19, 22), fill=accent)

    shadow = Image.new("RGBA", (S, S))
    ImageDraw.Draw(shadow).rounded_rectangle((35, 6, 95, 66), 10, fill=(0, 0, 0, 160))
    base = bg.convert("RGBA")
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(3)))
    base.alpha_composite(art.resize((56, 56)), (36, 4))

    # every frame costs ~1 s of upload, so the title is wrapped instead of scrolled
    d = ImageDraw.Draw(base)
    ft, fa = font(13, True), font(11)
    lines = wrap(clean(track.title), ft, S - 8, 2)
    y = 63 if len(lines) == 2 else 70
    for l in lines:
        d.text((S // 2, y), l, font=ft, fill="white", anchor="ma")
        y += 16
    artist = fit(clean(track.artist) or clean(track.app), fa, S - 12)
    d.text((S // 2, 96), artist, font=fa, fill=(200, 200, 210), anchor="ma")

    # pause/resume doesn't re-upload (each upload shows the keyboard's Loading screen),
    # so the picture is the same either way
    bars, n = 14, frames_n
    rnd = random.Random(track.title)
    frames = []
    for i in range(n):
        im = base.copy()
        d = ImageDraw.Draw(im)
        for b in range(bars):
            h = 2 + rnd.random() * 12
            bx = 8 + b * 8
            d.rounded_rectangle((bx, 125 - h, bx + 5, 125), 2, fill=accent)
        frames.append((im.convert("RGB"), 140))
    return frames


# ---------------------------------------------------------------- notifications

APPS = [  # (substring of the app name, label, color)
    ("teams", "T", (91, 95, 199)),
    ("discord", "D", (88, 101, 242)),
    ("outlook", "O", (15, 108, 189)),
    ("mail", "@", (0, 120, 212)),
    ("почта", "@", (0, 120, 212)),
    ("gmail", "M", (234, 67, 53)),
    ("telegram", "T", (42, 171, 238)),
    ("whatsapp", "W", (37, 211, 102)),
    ("slack", "S", (224, 30, 90)),
    ("steam", "S", (40, 70, 110)),
]


def app_style(app):
    a = app.lower()
    for key, label, color in APPS:
        if key in a:
            return label, color
    return (clean(app)[:1] or "!").upper(), (110, 110, 130)


def notification(notice, more=0, animated=False):
    label, color = app_style(notice.app)
    lines = [clean(l) for l in notice.lines] or [""]
    title, body = lines[0], " ".join(lines[1:])
    fh, ft, fb = font(11, True), font(13, True), font(11)

    base = Image.new("RGB", (S, S), tuple(c // 5 for c in color))
    d = ImageDraw.Draw(base)
    d.rectangle((0, 0, S, 34), fill=tuple(c // 2 for c in color))
    app = re.sub(r"^Microsoft\s+", "", clean(notice.app))
    d.text((38, 17), fit(app, fh, S - 44 - (18 if more else 0)), font=fh, fill="white", anchor="lm")
    if more:
        d.rounded_rectangle((S - 22, 9, S - 4, 25), 8, fill=(230, 50, 60))
        d.text((S - 13, 17), f"+{min(more, 9)}", font=font(10, True), fill="white", anchor="mm")
    y = 40
    for l in wrap(title, ft, S - 10, 2):
        d.text((5, y), l, font=ft, fill="white")
        y += 16
    for l in wrap(body, fb, S - 10, 5 - (y - 40) // 16):
        d.text((5, y + 2), l, font=fb, fill=(205, 205, 215))
        y += 14

    frames = []
    n = 4 if animated else 1  # ~1 s of upload per frame, and notifications should show up fast
    for i in range(n):
        im = base.copy()
        d = ImageDraw.Draw(im)
        r = 11 + 2 * i  # ring expanding out of the badge
        ring = tuple(int(c + (255 - c) * 0.3) for c in color)
        if i < n - 1:
            d.ellipse((18 - r, 17 - r, 18 + r, 17 + r), outline=ring, width=2)
        d.ellipse((7, 6, 29, 28), fill=color)
        d.text((18, 17), label, font=font(13, True), fill="white", anchor="mm")
        frames.append((im, 110))
    frames[-1] = (frames[-1][0], 500)
    return frames


# ---------------------------------------------------------------- idle animations

IDLE = {  # style -> title for the UI
    "quote": "Цитата и дата",
    "quote_clock": "Цитата, часы и дата",
    "matrix": "Матрица",
    "plasma": "Плазма",
    "stars": "Звёзды",
    "custom": "Своя картинка",
}


def idle(style="quote", seed=0, custom=None):
    if style in ("quote", "quote_clock"):
        return quote_card(seed, clock=style == "quote_clock")
    if style == "custom" and custom:
        return load_image(custom)
    return {"matrix": matrix, "plasma": plasma, "stars": stars}.get(style, matrix)()


WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]
PALETTES = [((40, 20, 90), (10, 60, 120)), ((90, 20, 60), (30, 10, 70)), ((10, 70, 70), (10, 30, 80)),
            ((80, 40, 10), (60, 10, 50)), ((20, 50, 20), (10, 40, 70)), ((60, 10, 10), (20, 20, 70))]


def quote_card(seed, clock=False, now=None):
    import datetime
    from .quotes import QUOTES
    now = now or datetime.datetime.now()
    c1, c2 = PALETTES[seed % len(PALETTES)]
    bg = Image.linear_gradient("L").resize((S, S)).rotate(30)
    im = ImageOps.colorize(bg, c1, c2)
    d = ImageDraw.Draw(im)

    date = f"{WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}"
    if clock:
        d.text((S // 2, 2), now.strftime("%H:%M"), font=font(30, True), fill="white", anchor="ma")
        d.text((S // 2, 37), date, font=font(10), fill=(220, 220, 235), anchor="ma")
        top = 58
    else:
        greet = ("Доброй ночи" if now.hour < 5 else "Доброе утро" if now.hour < 12
                 else "Добрый день" if now.hour < 18 else "Добрый вечер")
        d.text((S // 2, 6), greet, font=font(14, True), fill="white", anchor="ma")
        d.text((S // 2, 26), date, font=font(10), fill=(220, 220, 235), anchor="ma")
        top = 48
    d.line((44, top - 4, 84, top - 4), fill=tuple(min(255, c + 90) for c in c2), width=1)

    # largest font that fits the quote into the space left above the author line;
    # quotes too long even for the smallest font are skipped
    room = S - top - 18
    for k in range(len(QUOTES)):
        text, author = QUOTES[(seed + k) % len(QUOTES)]
        for size in (14, 13, 12, 11, 10):
            f = font(size, True)
            lines = wrap(f"«{clean(text)}»", f, S - 10, room // (size + 3))
            if not lines[-1].endswith("…"):
                break
        if not lines[-1].endswith("…"):
            break
    lh = size + 3
    y = top + (room - lh * len(lines)) // 2
    for l in lines:
        d.text((S // 2, y), l, font=f, fill="white", anchor="ma")
        y += lh
    d.text((S - 6, S - 4), f"— {author}", font=font(10), fill=(200, 200, 220), anchor="rd")
    return [(im, 500)]


def load_image(path):
    from itertools import islice
    from PIL import ImageSequence
    from .keyboard import MAX_FRAMES
    im = Image.open(path)
    # the iterator yields the same object seeked to each frame, so convert every frame while
    # iterating; collecting the iterator first would give N copies of the last frame
    return [(ImageOps.fit(fr.convert("RGB"), (S, S)), fr.info.get("duration", 100) or 100)
            for fr in islice(ImageSequence.Iterator(im), MAX_FRAMES)]


def matrix(n=12):
    rnd = random.Random(7)
    f = font(10, mono=True)
    cols = S // 8
    cell = 11
    rows = S // cell + 1
    glyphs = "01$#%&*+<>=ABCDEFXYZ0123456789"
    # each column falls an integer number of screens per loop, so the animation wraps
    columns = [(rnd.choice((1, 2)), rnd.random(), [rnd.choice(glyphs) for _ in range(rows * 2)]) for _ in range(cols)]
    frames = []
    for i in range(n):
        im = Image.new("RGB", (S, S), (0, 6, 2))
        d = ImageDraw.Draw(im)
        for c, (speed, offset, chars) in enumerate(columns):
            head = ((offset + speed * i / n) % 1) * (rows * 2) - rows // 2
            for r in range(rows):
                dist = head - r
                if not 0 <= dist < 9:
                    continue
                g = 255 if dist < 1 else int(220 * (1 - dist / 9))
                fill = (200, 255, 200) if dist < 1 else (0, g, int(g * 0.3))
                ch = chars[(r + i // 3) % len(chars)] if dist < 1 else chars[r]
                d.text((c * 8 + 1, r * cell), ch, font=f, fill=fill)
        frames.append((im, 70))
    return frames


def plasma(n=12):
    import numpy as np
    y, x = np.mgrid[0:S, 0:S] / S * math.tau
    frames = []
    for i in range(n):
        t = i / n * math.tau
        v = np.sin(x * 1.5 + t) + np.sin(y * 2 + t) + np.sin((x + y) * 1.2 + t) + np.sin(np.hypot(x - 3, y - 3) * 2 - t)
        h = (v / 8 + 0.5)
        rgb = np.stack([np.sin(h * math.tau + o) * 0.5 + 0.5 for o in (0, 2.1, 4.2)], -1)
        frames.append((Image.fromarray((rgb * 255).astype("uint8")), 90))
    return frames


def stars(n=12):
    rnd = random.Random(3)
    pts = [(rnd.uniform(-1, 1), rnd.uniform(-1, 1), rnd.random()) for _ in range(90)]
    frames = []
    for i in range(n):
        im = Image.new("RGB", (S, S), (2, 2, 12))
        d = ImageDraw.Draw(im)
        for x, y, z0 in pts:
            z = 1 - ((z0 + i / n) % 1)  # each star flies in once per loop
            sx, sy = 64 + x / (z + 0.05) * 20, 64 + y / (z + 0.05) * 20
            b = int(255 * (1 - z))
            r = 0 if z > 0.5 else 1
            d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=(b, b, min(255, b + 40)))
        frames.append((im, 80))
    return frames


def save_gif(frames, path, fmt=None):
    ims = [im for im, _ in frames]
    ims[0].save(path, format=fmt, save_all=True, append_images=ims[1:], duration=[ms for _, ms in frames], loop=0)
