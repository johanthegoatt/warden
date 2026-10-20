"""Keeps Warden running in the background on Windows.

  python scripts/watchdog.py start      start detached, no console window
  python scripts/watchdog.py stop       stop the watchdog, sensor and brain
  python scripts/watchdog.py status     show what is running and the last log lines
  python scripts/watchdog.py install    also start it at every logon (Startup folder shortcut)
  python scripts/watchdog.py uninstall  remove the Startup shortcut

The watchdog runs the sensor and the brain as child processes and restarts
either one if it exits, waiting longer after each crash up to five minutes.
The sensor runs from a copy in bin/, so rebuilding never fights a locked exe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
BIN = ROOT / "bin"
PIDFILE = LOGS / "watchdog.pid"
STOPFILE = LOGS / "watchdog.stop"
MAX_LOG = 5 * 1024 * 1024
NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def built_sensor() -> Path:
    for profile in ("release", "debug"):
        exe = ROOT / "sensor" / "target" / profile / "warden-sensor.exe"
        if exe.exists():
            return exe
    raise SystemExit("no sensor build found; run: cargo build --release --manifest-path sensor/Cargo.toml")


def pythonw() -> str:
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w if w.exists() else sys.executable)


def log(name: str, line: str) -> None:
    LOGS.mkdir(exist_ok=True)
    path = LOGS / f"{name}.log"
    rotate(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def rotate(path: Path) -> None:
    if path.exists() and path.stat().st_size > MAX_LOG:
        path.replace(path.with_suffix(".log.1"))


def alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                         creationflags=NO_WINDOW)
    return str(pid) in out.stdout


def running_pid():
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if alive(pid) else None


class Child:
    def __init__(self, name: str, cmd: list, cwd: Path):
        self.name, self.cmd, self.cwd = name, cmd, cwd
        self.proc = None
        self.crashes = 0
        self.next_start = 0.0
        self.started = 0.0

    def poll(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            if time.time() - self.started > 600:
                self.crashes = 0  # ten quiet minutes resets the backoff
            return
        if self.proc is not None:
            code = self.proc.returncode
            self.proc = None
            self.crashes += 1
            delay = min(300, 5 * 2 ** (self.crashes - 1))
            self.next_start = time.time() + delay
            log("watchdog", f"{self.name} exited with code {code}; restarting in {delay}s")
        if time.time() >= self.next_start:
            path = LOGS / f"{self.name}.log"
            rotate(path)
            out = path.open("a", encoding="utf-8")
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(ROOT / "brain")}
            self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdout=out, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW)
            out.close()
            self.started = time.time()
            log("watchdog", f"started {self.name} (pid {self.proc.pid})")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def run() -> int:
    LOGS.mkdir(exist_ok=True)
    if running_pid():
        log("watchdog", "another watchdog is already running; this one exits")
        return 0
    PIDFILE.write_text(str(os.getpid()))
    STOPFILE.unlink(missing_ok=True)
    BIN.mkdir(exist_ok=True)
    exe = BIN / "warden-sensor.exe"
    try:
        shutil.copy2(built_sensor(), exe)
    except PermissionError:
        log("watchdog", "bin/warden-sensor.exe is in use; running the existing copy")
    children = [
        Child("sensor", [str(exe), "run", "--config", str(ROOT / "warden.toml")], ROOT),
        Child("brain", [sys.executable, "-m", "warden_brain", "--model", os.environ.get("WARDEN_MODEL", "none"),
                        "--interval", "10", "--live", "--reports", str(ROOT / "incidents")], ROOT / "brain"),
    ]
    log("watchdog", f"watchdog {os.getpid()} up; dashboard on http://127.0.0.1:7878")
    try:
        while not STOPFILE.exists():
            for c in children:
                c.poll()
            time.sleep(2)
    finally:
        for c in children:
            c.stop()
        STOPFILE.unlink(missing_ok=True)
        PIDFILE.unlink(missing_ok=True)
        log("watchdog", "stopped")
    return 0


def start() -> int:
    pid = running_pid()
    if pid:
        print(f"already running (watchdog pid {pid}); dashboard http://127.0.0.1:7878")
        return 0
    built_sensor()
    subprocess.Popen([pythonw(), str(Path(__file__).resolve()), "run"], cwd=ROOT,
                     creationflags=DETACHED | NO_WINDOW, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    for _ in range(20):
        time.sleep(0.5)
        if running_pid():
            print(f"started (watchdog pid {running_pid()}); dashboard http://127.0.0.1:7878, logs in {LOGS}")
            return 0
    print(f"the watchdog did not come up; see {LOGS / 'watchdog.log'}")
    return 1


def stop() -> int:
    pid = running_pid()
    if not pid:
        print("not running")
        return 0
    STOPFILE.write_text("stop")
    for _ in range(30):
        time.sleep(0.5)
        if not alive(pid):
            print("stopped")
            return 0
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    PIDFILE.unlink(missing_ok=True)
    print("stopped (forced)")
    return 0


def status() -> int:
    pid = running_pid()
    print(f"watchdog: {'running, pid ' + str(pid) if pid else 'not running'}")
    print(f"starts at logon: {'yes' if shortcut().exists() else 'no'}")
    for name in ("watchdog", "sensor", "brain"):
        path = LOGS / f"{name}.log"
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-4:]
            print(f"\n{name}.log:\n  " + "\n  ".join(lines))
    return 0


def shortcut() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Warden.lnk"


def install() -> int:
    """A Startup-folder shortcut: runs at logon without the admin rights a Task Scheduler logon trigger needs."""
    lnk = shortcut()
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:WARDEN_LNK);"
        "$s.TargetPath = $env:WARDEN_PYW; $s.Arguments = $env:WARDEN_ARGS;"
        "$s.WorkingDirectory = $env:WARDEN_ROOT; $s.Description = 'Warden watchdog'; $s.Save()"
    )
    env = {**os.environ, "WARDEN_LNK": str(lnk), "WARDEN_PYW": pythonw(),
           "WARDEN_ARGS": f'"{Path(__file__).resolve()}" run', "WARDEN_ROOT": str(ROOT)}
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, env=env)
    if out.returncode != 0 or not lnk.exists():
        print(f"could not create {lnk}: {out.stderr.strip()}")
        return 1
    print(f"Warden now starts at every logon ({lnk}).")
    return start()


def uninstall() -> int:
    lnk = shortcut()
    if lnk.exists():
        lnk.unlink()
        print("removed from Startup")
    else:
        print("was not in Startup")
    return 0


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("watchdog.py targets Windows; on Linux use a systemd unit instead")
    commands = {"run": run, "start": start, "stop": stop, "status": status, "install": install, "uninstall": uninstall}
    arg = sys.argv[1] if len(sys.argv) > 1 else "status"
    if arg not in commands:
        raise SystemExit(__doc__)
    sys.exit(commands[arg]())
