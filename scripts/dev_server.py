"""Start, stop and check the development API server.

Wraps uvicorn so the server can be managed from one place on any platform,
including Windows where process handling differs from Unix.

    python scripts/dev_server.py start
    python scripts/dev_server.py status
    python scripts/dev_server.py stop
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
PID_FILE = REPO_ROOT / "data" / ".dev_server.pid"
LOG_FILE = REPO_ROOT / "data" / "dev_server.log"
DEFAULT_PORT = 8000


def _python() -> str:
    for candidate in ("venv/Scripts/python.exe", "venv/bin/python"):
        path = REPO_ROOT / candidate
        if path.exists():
            return str(path)
    return sys.executable


def _health(port: int, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def start(port: int, reload: bool) -> int:
    if _health(port):
        print(f"Already running on :{port}")
        return 0

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _python(),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if reload:
        command.append("--reload")

    with open(LOG_FILE, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=BACKEND_DIR,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    PID_FILE.write_text(str(process.pid), encoding="utf-8")

    # Poll rather than sleeping a fixed amount: startup time varies with how
    # much of the model/ORM metadata has to be imported.
    for _ in range(60):
        if process.poll() is not None:
            print(f"Server exited immediately. Log:\n{LOG_FILE.read_text(encoding='utf-8')}")
            return 1
        health = _health(port, timeout=1.0)
        if health:
            print(f"Started on :{port} (pid {process.pid})")
            print(
                f"  status={health['status']} database={health['database']} redis={health['redis']}"
            )
            return 0
        time.sleep(0.5)

    print(
        f"Did not become healthy within 30s. Log:\n{LOG_FILE.read_text(encoding='utf-8')[-2000:]}"
    )
    return 1


def stop() -> int:
    if not PID_FILE.exists():
        print("No pid file; nothing to stop.")
        return 0
    pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                check=False,
                capture_output=True,
            )
        else:
            os.kill(pid, signal.SIGTERM)
        print(f"Stopped pid {pid}")
    except (ProcessLookupError, OSError) as exc:
        print(f"Could not stop pid {pid}: {exc}")
    PID_FILE.unlink(missing_ok=True)
    return 0


def status(port: int) -> int:
    health = _health(port)
    if health is None:
        print(f"Not running on :{port}")
        return 1
    print(json.dumps(health, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "restart"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.action == "start":
        return start(args.port, args.reload)
    if args.action == "stop":
        return stop()
    if args.action == "restart":
        stop()
        time.sleep(1)
        return start(args.port, args.reload)
    return status(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
