"""
Single-file launcher for the Azure BOM Region Dashboard.

This is the entry point used by the packaged Windows executable (PyInstaller)
and can also be run directly with ``python launch.py``. It:

  * sets sensible local defaults (LOCAL_MODE, host/port, CSRF origin),
  * starts the FastAPI/uvicorn host in-process,
  * opens the dashboard in the default browser.

Environment overrides (all optional):
  PORT=4280            listening port
  HOST=127.0.0.1       bind address
  DEMO_MODE=true       seed sample data + show a demo banner
  NO_BROWSER=1         don't auto-open the browser
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _repo_root() -> Path:
    # When frozen by PyInstaller, resources are unpacked next to the exe in
    # sys._MEIPASS; otherwise use this file's directory.
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    return Path(__file__).resolve().parent


def main() -> None:
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    port = int(os.environ.get("PORT", "4280") or "4280")
    host = os.environ.get("HOST", "127.0.0.1")

    os.environ.setdefault("LOCAL_MODE", "true")
    os.environ.setdefault("ALLOWED_ORIGIN", f"http://localhost:{port}")

    # Keep local state next to the executable/repo by default so a customer can
    # find (and back up or wipe) it easily.
    os.environ.setdefault("LOCAL_STORAGE_DIR", str(root / "local-storage"))

    url = f"http://localhost:{port}/"
    if os.environ.get("NO_BROWSER", "").lower() not in ("1", "true", "yes"):
        def _open():
            time.sleep(2.0)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    print("=" * 63)
    print("  Azure BOM Region Support Dashboard")
    print(f"  Dashboard:   {url}")
    print(f"  Storage:     {os.environ['LOCAL_STORAGE_DIR']}")
    if os.environ.get("DEMO_MODE", "").lower() in ("1", "true", "yes"):
        print("  Demo mode:   ON (sample data seeded)")
    print("  Press Ctrl+C to stop.")
    print("=" * 63)

    import uvicorn
    from server.app import app

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
