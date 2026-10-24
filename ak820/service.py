"""Background service: decides what the screen shows, uploads it, runs the lighting, and keeps
the settings. The window (gui.py) only reads status() and calls the action methods."""
import asyncio
import base64
import collections
import hashlib
import io
import json
import threading
import time
from pathlib import Path

from . import render
from .keyboard import Keyboard, NotConnected, screen_blob
from .lights import Lights, hex_rgb
from .sources import Media, Notice, Notifications

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
SCREEN_STATE = ROOT / ".screen_state"  # hash of the picture last uploaded in full

DEFAULTS = {
    "screen_music": True,
    "music_sources": "music",  # "music": music players only; "all": browsers and video players too
    "pause_to_idle_seconds": 120,
    "screen_notifications": True,
    "notify_apps": ["outlook", "mail", "почта", "teams", "discord", "telegram", "whatsapp", "slack", "gmail"],
    "notice_seconds": 20,
    "key_flash_on_notice": True,
    "idle_style": "quote",
    "idle_custom": "",
    "quote_minutes": 30,
    "clock_minutes": 5,
    "animated_screens": False,
    "settle_seconds": 4,
    "max_uploads_per_hour": 30,
    "light_mode": "wave",
    "light_color": "#7c5cff",
    "light_color2": "#00e0ff",
    "light_speed": 3,
    "light_brightness": 100,
    "visualizer_style": "rainbow",
    "hw_mode": 8,
    "hw_color": "#ff2060",
    "hw_rainbow": True,
    "hw_brightness": 5,
    "hw_speed": 3,
    "hw_direction": 0,
    "custom_keys": {},
    "sync_time_on_start": True,
    "start_minimized": False,
    "keyboard_load": "normal",  # "gentle": fewer screen uploads and lighter live lighting
}

MUSIC_APPS = ("music", "spotify", "aimp", "foobar", "vlc", "winamp", "deezer", "tidal", "itunes", "soundcloud")
# Uploading takes ~1 s per frame and the keyboard shows "Loading" meanwhile
UPLOADING = threading.Lock()


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        except ValueError:
            saved = {}
        # settings files from the first version used these names
        renames = {"idle_animation": "idle_style"}
        cfg.update({renames.get(k, k): v for k, v in saved.items() if renames.get(k, k) in DEFAULTS})
        if "pause_to_idle_minutes" in saved and "pause_to_idle_seconds" not in saved:
            cfg["pause_to_idle_seconds"] = saved["pause_to_idle_minutes"] * 60
    if cfg["idle_style"] not in render.IDLE:
        cfg["idle_style"] = "quote"
    return cfg


class Service:
    def __init__(self):
        self.cfg = load_config()
        self.kb = None
        self.connected = False
        self.logs = collections.deque(maxlen=200)
        self.progress = None  # (done, total) while uploading
        self.preview = None  # data URL of what the screen shows
        self.screen_title = ""
        self.track = None
        self.uploads = []
        self.shown = None
        self.want, self.want_since = None, 0.0
        self.notice, self.notice_more, self.notice_until = None, 0, 0.0
        self.paused_since = None
        self.force = False
        self.lights = Lights(lambda: self.kb, lambda: self.cfg, self.log)

    # ------------------------------------------------------------ plumbing

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.logs.append(line)
        print(line, flush=True)
        try:
            with open(ROOT / "ak820.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def save(self):
        CONFIG.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._loop()), daemon=True).start()
        self.lights.start()

    def background(self, fn, *args):
        def run():
            try:
                fn(*args)
            except Exception as e:
                self.log(f"ошибка: {e}")
        threading.Thread(target=run, daemon=True).start()

    def connect(self):
        if self.kb:
            return
        try:
            self.kb = Keyboard()
        except (NotConnected, OSError) as e:
            if self.connected is not None:
                self.log(str(e).replace("AK820 Pro is on Bluetooth: plug in the USB cable",
                                        "Клавиатура подключена по Bluetooth — подключите USB-кабель")
                         .replace("AK820 Pro not found: plug it in with the USB cable",
                                  "Клавиатура не найдена — подключите её USB-кабелем"))
            self.connected = None
            return
        self.connected = True
        self.shown = None
        self.log("клавиатура подключена")
        if self.cfg["sync_time_on_start"]:
            self.kb.sync_time()
            self.log("часы клавиатуры синхронизированы")

    def lost(self, e):
        self.log(f"связь с клавиатурой потеряна ({e})")
        self.kb, self.connected, self.shown = None, None, None

    # ------------------------------------------------------------ what to show

    def is_music(self, track):
        return self.cfg["music_sources"] == "all" or any(a in track.app.lower() for a in MUSIC_APPS)

    def limits(self):
        """Settings after the gentle mode's caps. Gentle mode trades freshness for fewer flash
        writes and less USB traffic; the keyboard's own memory and chip can't be extended."""
        cfg = self.cfg
        if cfg["keyboard_load"] != "gentle":
            return {"uploads": cfg["max_uploads_per_hour"], "settle": cfg["settle_seconds"],
                    "clock": cfg["clock_minutes"], "animated": cfg["animated_screens"], "count_more": True}
        return {"uploads": min(cfg["max_uploads_per_hour"], 15), "settle": max(cfg["settle_seconds"], 8),
                "clock": max(cfg["clock_minutes"], 10), "animated": False, "count_more": False}

    def desired(self):
        cfg, lim, now = self.cfg, self.limits(), time.time()
        if self.notice and time.monotonic() < self.notice_until:
            # in gentle mode a burst of messages from one chat doesn't re-upload just for "+N"
            n, more = self.notice, self.notice_more if lim["count_more"] else 0
            return ("notice", n.id if lim["count_more"] else n.app, more), f"Уведомление: {n.app}", \
                lambda: render.notification(n, more, lim["animated"])
        t = self.track
        if cfg["screen_music"] and t and self.is_music(t):
            idle_after = cfg["pause_to_idle_seconds"]
            if t.playing or not idle_after or (self.paused_since and now - self.paused_since < idle_after):
                return ("track", t.title, t.artist), f"Музыка: {t.title}", \
                    lambda: render.now_playing(t, 6 if lim["animated"] else 1)
        style = cfg["idle_style"]
        seed = int(now // (max(1, cfg["quote_minutes"]) * 60))
        tick = int(now // (max(1, lim["clock"]) * 60)) if style == "quote_clock" else 0
        day = time.strftime("%Y%m%d") if style.startswith("quote") else ""
        return ("idle", style, seed, tick, day, cfg["idle_custom"]), render.IDLE[style], \
            lambda: render.idle(style, seed, cfg["idle_custom"])

    def may_upload(self, urgent):
        now = time.monotonic()
        self.uploads = [t for t in self.uploads if now - t < 3600]
        if len(self.uploads) >= self.limits()["uploads"] and not self.force:
            return False
        return urgent or self.force or not self.uploads or now - self.uploads[-1] >= 8

    def show(self, key, title, make):
        frames = make()
        buf = io.BytesIO()
        render.save_gif(frames, buf, fmt="GIF")
        self.preview = "data:image/gif;base64," + base64.b64encode(buf.getvalue()).decode()
        blob = screen_blob(frames)
        digest = hashlib.sha1(blob).hexdigest()
        if digest == self.last_upload() and not self.force:
            # the keyboard already holds exactly this picture (e.g. after restarting the program)
            self.log(f"экран: {title} — уже на клавиатуре, загрузка не нужна")
            self.shown, self.screen_title = key, title
            return
        t = time.monotonic()
        self.log(f"экран: {title}")
        with UPLOADING:
            self.progress = (0, 1)
            SCREEN_STATE.unlink(missing_ok=True)  # an interrupted upload must not count as done
            try:
                self.kb.upload_screen(blob, lambda i, n: setattr(self, "progress", (i, n)))
            finally:
                self.progress = None
            SCREEN_STATE.write_text(digest)
        self.log(f"экран обновлён за {time.monotonic() - t:.1f} с ({len(frames)} кадр.)")
        self.uploads.append(time.monotonic())
        self.shown, self.screen_title, self.force = key, title, False

    def last_upload(self):
        try:
            return SCREEN_STATE.read_text().strip()
        except OSError:
            return None

    async def _loop(self):
        media, notes = Media(), Notifications()
        try:
            await notes.start()
        except Exception as e:
            self.log(f"уведомления недоступны: {e}")
            notes = None
        loop = asyncio.get_running_loop()
        while True:
            self.connect()
            try:
                self.track = await media.current()
            except Exception:
                self.track = None
            if self.track and not self.track.playing:
                self.paused_since = self.paused_since or time.time()
            else:
                self.paused_since = None
            if notes:
                try:
                    fresh = [n for n in await notes.new() if self.wants_app(n.app)]
                except Exception:
                    fresh = []
                for n in fresh:
                    self.on_notice(n)

            key, title, make = self.desired()
            if key != self.want:
                self.want, self.want_since = key, time.monotonic()
            urgent = key[0] == "notice"
            settled = urgent or time.monotonic() - self.want_since >= self.limits()["settle"]
            if self.kb and (key != self.shown or self.force) and settled and self.may_upload(urgent):
                try:
                    await loop.run_in_executor(None, self.show, key, title, make)
                except OSError as e:
                    self.lost(e)
                except Exception as e:
                    self.log(f"ошибка экрана: {e}")
                    self.shown = key  # don't retry a broken picture every second
            await asyncio.sleep(1)

    def wants_app(self, app):
        apps = self.cfg["notify_apps"]
        return not apps or any(a.lower() in app.lower() for a in apps)

    def on_notice(self, n):
        self.log(f"уведомление: {n.app}: {' | '.join(n.lines)[:70]}")
        if self.cfg["key_flash_on_notice"]:
            self.lights.flash(render.app_style(n.app)[1])
        if not self.cfg["screen_notifications"]:
            return
        active = self.notice and time.monotonic() < self.notice_until
        self.notice_more = self.notice_more + 1 if active else 0
        self.notice = n
        self.notice_until = time.monotonic() + self.cfg["notice_seconds"]

    # ------------------------------------------------------------ actions for the window

    def status(self):
        t = self.track
        return {
            "connected": bool(self.kb),
            "progress": self.progress,
            "preview": self.preview,
            "screen": self.screen_title,
            "track": {"title": t.title, "artist": t.artist, "app": t.app, "playing": t.playing,
                      "counted": self.is_music(t)} if t else None,
            "uploads_hour": len([u for u in self.uploads if time.monotonic() - u < 3600]),
            "uploads_limit": self.limits()["uploads"],
            "logs": list(self.logs)[-60:],
        }

    def update(self, changes):
        changes = {k: v for k, v in changes.items() if k in DEFAULTS}
        self.cfg = {**self.cfg, **changes}
        self.save()

    def refresh_screen(self):
        self.force = True

    def test_notice(self, app):
        self.on_notice(Notice(int(time.time()), app, ["Тестовое уведомление", "Так будут выглядеть сообщения на экране клавиатуры"]))

    def sync_time(self):
        if self.kb:
            self.background(lambda: (self.kb.sync_time(), self.log("часы клавиатуры синхронизированы")))

    def apply_hardware(self):
        c = self.cfg
        self.update({"light_mode": "hardware"})
        if self.kb:
            rgb = None if c["hw_rainbow"] else hex_rgb(c["hw_color"])
            self.background(lambda: (self.kb.set_mode(c["hw_mode"], rgb, c["hw_brightness"], c["hw_speed"],
                                                      c["hw_direction"]), self.log("встроенный эффект применён")))

    def apply_keys(self, colors):
        self.update({"light_mode": "hardware", "custom_keys": colors})
        if self.kb:
            rgb = {int(i): hex_rgb(c) for i, c in colors.items()}
            self.background(lambda: (self.kb.set_keys(rgb, self.cfg["hw_brightness"]),
                                     self.log("свои цвета клавиш сохранены в клавиатуру")))
