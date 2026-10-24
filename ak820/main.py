"""Entry point: `python -m ak820` opens AK820 Studio; the other commands are for the console."""
import argparse
import sys
import time

from .keyboard import Keyboard, NotConnected, screen_blob


def main():
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="ak820")
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("gui", help="AK820 Studio window (default)")
    g.add_argument("--minimized", action="store_true", help="start hidden in the tray")
    sub.add_parser("run", help="the same service without a window, logging to the console")
    sub.add_parser("time", help="sync the keyboard clock")
    i = sub.add_parser("image", help="upload an image or GIF to the screen")
    i.add_argument("path")
    args = ap.parse_args()

    if args.cmd in (None, "gui"):
        from .gui import run
        run(minimized=getattr(args, "minimized", False))
        return
    if args.cmd == "run":
        from .service import Service
        Service().start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return
    try:
        kb = Keyboard()
    except NotConnected as e:
        sys.exit(str(e))
    if args.cmd == "time":
        kb.sync_time()
        print("clock synced")
    elif args.cmd == "image":
        from .render import load_image
        frames = load_image(args.path)
        kb.upload_screen(screen_blob(frames), lambda a, n: print(f"\r{a}/{n}", end="", flush=True))
        print(f"\nuploaded {len(frames)} frames")
    kb.close()
