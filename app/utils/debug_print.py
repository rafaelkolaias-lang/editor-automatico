import os
import threading
from datetime import datetime


def _debug_enabled() -> bool:
    v = (os.environ.get("APP_DEBUG", "1") or "").strip().lower()
    return v not in ("0", "false", "no", "off", "")


def debug_print(tag: str, message: str, **data):
    if not _debug_enabled():
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    thread_name = threading.current_thread().name

    extras = ""
    if data:
        extras = " | " + " ".join(f"{k}={v!r}" for k, v in data.items())

    print(f"[{ts}] [{thread_name}] [{tag}] {message}{extras}", flush=True)
