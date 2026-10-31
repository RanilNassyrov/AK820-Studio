"""Real FPS of the game in the foreground, measured by Intel PresentMon (tools/PresentMon.exe).

PresentMon reads the frames each application presents from Windows' event tracing. It needs no
administrator rights for users in the "Performance Log Users" group, and it counts frames above
the monitor's refresh rate, in exclusive fullscreen too. Its output is one CSV row per frame.
"""
import atexit
import collections
import ctypes
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

EXE = Path(__file__).resolve().parent.parent / "tools" / "PresentMon.exe"
SESSION = "AK820StudioFps"
WINDOW_MS = 1000
STALE = 1.5  # seconds without frames before a game counts as gone
NO_WINDOW = 0x08000000

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)


def foreground_pid():
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value


def kill_with_parent(proc):
    """Put proc in a job that Windows closes when this program dies, however it dies."""
    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: 144 bytes on x64, LimitFlags at offset 16
    info = (ctypes.c_ubyte * 144)()
    ctypes.c_uint32.from_buffer(info, 16).value = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    job = kernel32.CreateJobObjectW(None, None)
    kernel32.SetInformationJobObject(job, 9, info, ctypes.sizeof(info))  # ExtendedLimitInformation
    kernel32.AssignProcessToJobObject(job, int(proc._handle))
    return job  # the handle must stay open for as long as proc runs


class GameFps:
    def __init__(self, exe=EXE, foreground=foreground_pid):
        self.exe, self.foreground = Path(exe), foreground
        self.wanted = False
        self.thread = self.proc = self.job = None
        self.error = None
        self.lock = threading.Lock()
        self.chains = {}  # (pid, swap chain) -> [app name, deque of frame times in ms, arrival time]
        atexit.register(self.stop_session)

    def want(self, on):
        if on == self.wanted:
            return
        self.wanted = on
        if on and not self.thread:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        elif not on and self.proc:
            self.proc.kill()

    def current(self):
        """(app name, fps) of the foreground application, or None if it isn't drawing frames."""
        pid, now, best = self.foreground(), time.monotonic(), None
        with self.lock:
            for (chain_pid, _), (name, times, seen) in self.chains.items():
                if chain_pid != pid or now - seen > STALE or len(times) < 2 or times[-1] <= times[0]:
                    continue
                fps = round((len(times) - 1) * 1000 / (times[-1] - times[0]))
                if not best or fps > best[1]:  # launchers keep extra swap chains: take the busiest
                    best = (name, fps)
        return best

    def stop_session(self):
        self.wanted = False
        if self.proc:
            self.proc.kill()
        if self.exe.exists():
            subprocess.run([str(self.exe), "--terminate_existing_session", "--session_name", SESSION],
                           creationflags=NO_WINDOW, capture_output=True, timeout=5, check=False)

    def _run(self):
        while self.wanted:
            if not self.exe.exists():
                self.error = "Нет файла tools\\PresentMon.exe: запустите install.bat"
                self.thread = None
                return
            self._capture()
            if self.wanted:
                time.sleep(10)  # the session dropped: try again
        self.thread = None

    def _capture(self):
        cmd = [str(self.exe), "--output_stdout", "--no_console_stats", "--stop_existing_session",
               "--session_name", SESSION, "--no_track_display", "--no_track_input", "--no_track_gpu",
               "--exclude", "dwm.exe"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        self.job = kill_with_parent(self.proc)
        columns = None
        for line in self.proc.stdout:
            if columns is None:
                if line.startswith("Application,"):
                    head = line.rstrip().split(",")
                    columns = [head.index(c) for c in ("Application", "ProcessID", "SwapChainAddress", "TimeInMs")]
                    self.error = None
                elif "error" in line.lower() or "denied" in line.lower():
                    self.error = line.strip()
                continue
            self._frame(line.split(","), columns)
        self.proc.wait()
        self.proc = None
        with self.lock:
            self.chains.clear()

    def _frame(self, row, columns):
        try:
            name, pid, chain, at = (row[i] for i in columns)
            pid, at = int(pid), float(at)
        except (IndexError, ValueError):
            return
        with self.lock:
            entry = self.chains.setdefault((pid, chain), [name, collections.deque(), 0.0])
            times = entry[1]
            times.append(at)
            while at - times[0] > WINDOW_MS:
                times.popleft()
            entry[2] = time.monotonic()
            if name != "<unknown>":
                entry[0] = name
            if len(self.chains) > 256:  # long-gone processes
                now = time.monotonic()
                self.chains = {k: v for k, v in self.chains.items() if now - v[2] < 5}
