"""What's playing and new notifications, read from Windows (no app-specific integrations)."""
from dataclasses import dataclass, field

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
)
from winrt.windows.storage.streams import Buffer, InputStreamOptions
from winrt.windows.ui.notifications import KnownNotificationBindings, NotificationKinds
from winrt.windows.ui.notifications.management import UserNotificationListener, UserNotificationListenerAccessStatus


@dataclass
class Track:
    title: str
    artist: str
    app: str
    playing: bool
    cover: bytes | None = field(default=None, repr=False)

    @property
    def key(self):
        return self.title, self.artist


@dataclass
class Notice:
    id: int
    app: str
    lines: list[str]


class Media:
    def __init__(self):
        self.mgr = None

    async def current(self):
        if self.mgr is None:
            self.mgr = await MediaManager.request_async()
        s = self.mgr.get_current_session()
        if not s:
            return None
        p = await s.try_get_media_properties_async()
        if not p or not p.title:
            return None
        info = s.get_playback_info()
        playing = info is not None and info.playback_status == Status.PLAYING
        return Track(p.title, p.artist or p.album_artist or "", s.source_app_user_model_id or "", playing,
                     await _read(p.thumbnail))


async def _read(ref, limit=4 << 20):
    if ref is None:
        return None
    try:
        stream = await ref.open_read_async()
        buf = Buffer(min(stream.size, limit))
        buf = await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
        return bytes(buf)
    except OSError:
        return None


class Notifications:
    """Polls the Windows notification center and returns toasts that appeared since the last call."""

    def __init__(self):
        self.listener = UserNotificationListener.current
        self.seen = None

    async def start(self):
        status = await self.listener.request_access_async()
        if status != UserNotificationListenerAccessStatus.ALLOWED:
            raise PermissionError("Windows denied access to notifications: Settings > Privacy > Notifications")
        self.seen = {n.id for n in await self.listener.get_notifications_async(NotificationKinds.TOAST)}

    async def new(self):
        out = []
        current = await self.listener.get_notifications_async(NotificationKinds.TOAST)
        for n in current:
            if n.id in self.seen:
                continue
            binding = n.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
            lines = [t.text for t in binding.get_text_elements()] if binding else []
            out.append(Notice(n.id, n.app_info.display_info.display_name, [l for l in lines if l.strip()]))
        self.seen = {n.id for n in current}
        return out
