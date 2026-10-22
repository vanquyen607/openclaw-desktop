"""Shared Gateway helpers + subprocess job engine for OpenClaw Desktop."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

GATEWAY_HTTP = "http://127.0.0.1:18789"
GATEWAY_HEALTH = f"{GATEWAY_HTTP}/health"
CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
LOG_DIR = Path(os.environ.get("TEMP", ".")) / "openclaw"
BACKUP_DIR = Path.home() / ".openclaw" / "backups"
WORKSPACE = Path.home() / ".openclaw" / "workspace"

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# `openclaw` on Windows is an npm .CMD shim; CreateProcess cannot run bare
# "openclaw". Resolve the real path once.
import shutil

_cli_path: str | None = None


def cli_path() -> str:
    global _cli_path
    if _cli_path is None:
        found = shutil.which("openclaw") or shutil.which("openclaw.cmd")
        if not found:
            npm = Path(os.environ.get("APPDATA", "")) / "npm"
            for name in ("openclaw.CMD", "openclaw.cmd"):
                cand = npm / name
                if cand.is_file():
                    found = str(cand)
                    break
        _cli_path = found or "openclaw"
    return _cli_path


# --------------------------------------------------------------------------- #
# Subprocess
# --------------------------------------------------------------------------- #
def run_cli(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [cli_path(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def run_cli_raw(argv: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


# --------------------------------------------------------------------------- #
# Gateway lifecycle
# --------------------------------------------------------------------------- #
def gateway_alive() -> bool:
    try:
        with urllib.request.urlopen(GATEWAY_HEALTH, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


_version_cache: str | None = None


def gateway_version() -> str:
    global _version_cache
    if _version_cache:
        return _version_cache
    try:
        proc = run_cli(["--version"], timeout=20)
        _version_cache = (proc.stdout or proc.stderr or "").strip()
    except Exception:
        _version_cache = "unknown"
    return _version_cache


def ensure_gateway() -> bool:
    if gateway_alive():
        return True
    try:
        run_cli(["gateway", "start", "--yes"], timeout=90)
    except Exception:
        pass
    for _ in range(30):
        if gateway_alive():
            return True
        time.sleep(1)
    return gateway_alive()


def restart_gateway() -> bool:
    try:
        run_cli(["gateway", "restart"], timeout=60)
    except Exception:
        return False
    for _ in range(30):
        if gateway_alive():
            return True
        time.sleep(1)
    return gateway_alive()


# --------------------------------------------------------------------------- #
# OmniRoute (LLM proxy — provider "omniroute" points at localhost:20128)
# --------------------------------------------------------------------------- #
OMNIRoute_BASE = "http://127.0.0.1:20128"

_omniroute_cli: str | None = None


def omniroute_alive() -> bool:
    """True when something answers on the OmniRoute port (401/404 still = alive)."""
    try:
        urllib.request.urlopen(OMNIRoute_BASE + "/v1/models", timeout=3)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def omniroute_cli_path() -> str | None:
    global _omniroute_cli
    if _omniroute_cli:
        return _omniroute_cli
    found = shutil.which("omniroute") or shutil.which("omniroute.cmd")
    if not found:
        npm = Path(os.environ.get("APPDATA", "")) / "npm"
        for name in ("omniroute.CMD", "omniroute.cmd"):
            cand = npm / name
            if cand.is_file():
                found = str(cand)
                break
    _omniroute_cli = found
    return found


def ensure_omniroute(timeout: int = 30) -> bool:
    """Start OmniRoute in the background if its proxy port is not up."""
    if omniroute_alive():
        return True
    exe = omniroute_cli_path()
    if not exe:
        return False
    try:
        subprocess.Popen(
            [exe, "serve", "--daemon", "--no-open", "--no-tray"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            cwd=str(Path.home()),
            close_fds=True,
        )
    except Exception:
        return False
    for _ in range(timeout):
        if omniroute_alive():
            return True
        time.sleep(1)
    return omniroute_alive()


def dashboard_info() -> dict | None:
    """Parse `openclaw dashboard --json --no-open` output robustly."""
    try:
        proc = run_cli(["dashboard", "--json", "--no-open"], timeout=60)
        raw = (proc.stdout or "").strip()
        if not raw:
            return None
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        return json.loads(raw[start : end + 1])
    except Exception:
        return None


def fresh_url() -> str:
    """Best pairing URL: single-use bootstrap > shared-token hash > plain."""
    info = dashboard_info()
    if info:
        if info.get("browserUrl"):
            return info["browserUrl"]
        if info.get("url"):
            return info["url"]
    return GATEWAY_HTTP + "/"


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def parse_json(raw: str | None):
    """Best-effort JSON extraction from CLI output (tolerates surrounding noise)."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    idxs = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    for i in sorted(idxs):
        close = "}" if s[i] == "{" else "]"
        j = s.rfind(close)
        if j > i:
            try:
                return json.loads(s[i : j + 1])
            except Exception:
                continue
    return None


def read_config() -> dict:
    return json.loads(read_config_text())


def write_config(data: dict) -> dict:
    return save_config_text(json.dumps(data, indent=2, ensure_ascii=False))


def read_config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


def save_config_text(text: str) -> dict:
    """Write config, validate via CLI, rollback on schema failure."""
    try:
        json.loads(text)
    except Exception as exc:
        return {"ok": False, "error": f"JSON syntax error: {exc}"}

    old = read_config_text()
    CONFIG_PATH.write_text(text, encoding="utf-8")
    try:
        proc = run_cli(["config", "validate", "--json"], timeout=60)
        raw = (proc.stdout or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        result = json.loads(raw[start : end + 1]) if start >= 0 and end > start else {}
        if result.get("valid"):
            return {"ok": True, "warnings": result.get("warnings", [])}
        CONFIG_PATH.write_text(old, encoding="utf-8")
        return {"ok": False, "error": result.get("error") or raw or "validation failed"}
    except Exception as exc:
        CONFIG_PATH.write_text(old, encoding="utf-8")
        return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #
def list_logs() -> list[dict]:
    files = []
    if LOG_DIR.is_dir():
        for p in LOG_DIR.glob("*.log"):
            try:
                st = p.stat()
                files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                pass
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files[:20]


def read_log_tail(name: str, lines: int = 300) -> str:
    path = (LOG_DIR / os.path.basename(name)).resolve()
    if not str(path).startswith(str(LOG_DIR.resolve())) or not path.is_file():
        return "[log file not found]"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            dq = deque(fh, maxlen=lines)
        return "".join(dq)
    except OSError as exc:
        return f"[error reading log: {exc}]"


# --------------------------------------------------------------------------- #
# Async job engine (long CLI runs with streaming output)
# --------------------------------------------------------------------------- #
_MAX_JOB_LINES = 5000
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def start_job(argv: list[str]) -> str | None:
    """Run openclaw argv in background, capture merged output line-stream."""
    if not argv or argv[0] != "openclaw":
        return None
    import uuid

    jid = uuid.uuid4().hex[:12]
    job = {"argv": argv, "lines": [], "done": False, "code": None, "t0": time.time()}
    with _jobs_lock:
        _jobs[jid] = job
        # prune old finished jobs
        dead = [k for k, v in _jobs.items() if v["done"] and time.time() - v["t0"] > 3600]
        for k in dead:
            _jobs.pop(k, None)

    def _run():
        try:
            proc = subprocess.Popen(
                [cli_path(), *argv[1:]],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                job["lines"].append(line.rstrip("\n"))
                if len(job["lines"]) > _MAX_JOB_LINES:
                    extra = len(job["lines"]) - _MAX_JOB_LINES
                    del job["lines"][:extra]
            job["code"] = proc.wait()
        except Exception as exc:
            job["lines"].append(f"[error] {exc}")
            job["code"] = -1
        finally:
            job["done"] = True

    threading.Thread(target=_run, daemon=True).start()
    return jid


def poll_job(jid: str, since: int) -> dict:
    with _jobs_lock:
        job = _jobs.get(jid)
        if job is None:
            return {"exists": False}
        lines = list(job["lines"][since:])
        total = len(job["lines"])
        done, code = job["done"], job["code"]
    return {"exists": True, "lines": lines, "total": total, "done": done, "code": code}


def kill_job(jid: str) -> bool:
    # jobs are short-lived CLI runs; killing would need the Popen handle.
    # Keep a reference for potential future use.
    with _jobs_lock:
        job = _jobs.get(jid)
    if job and not job["done"]:
        return False
    return True


# --------------------------------------------------------------------------- #
# Shell-less explorer helpers
# --------------------------------------------------------------------------- #
OPEN_TARGETS = {
    "backups": BACKUP_DIR,
    "logs": LOG_DIR,
    "workspace": WORKSPACE,
    "config": CONFIG_PATH,
    "openclaw": Path.home() / ".openclaw",
}


def open_explorer(target: str) -> bool:
    path = OPEN_TARGETS.get(target)
    if path is None:
        return False
    try:
        if path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)], creationflags=CREATE_NO_WINDOW)
        else:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        return True
    except Exception:
        return False
