"""Invoke the MediaCrawler CLI as a black box via subprocess.

Design notes
------------
* We do NOT modify MediaCrawler's source. To pass keywords / post-ids /
  creator-ids (which MediaCrawler reads from ``config/base_config.py``), we
  temporarily patch that file before the run and restore it afterwards. This is
  the version-robust approach because the CLI does not reliably accept these as
  flags across releases.
* Safety guardrails (delay window, comment toggle, CDP, max count) are written
  into the same config patch.
* One crawl runs at a time on this machine. Every project shares one config file,
  one debug Chrome and one set of per-day result files, so ``crawl_lock`` queues
  crawls across all MCP server processes. The crawler inherits the lock, so a
  crawl that outlives its server still holds its place.
* Logs go to temporary files, not pipes. Playwright's Node driver inherits stderr
  and makes it non-blocking; on a pipe, a long log line is then cut mid-character
  and later lines are dropped, including the ones that say why a run stopped.
* Command shape follows the documented CLI:
      uv run main.py --platform xhs --lt qrcode --type search --save_data_option json
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Iterator

from .config import Settings
from .risk_state import ANON_ONLY_MARKER, CDP_FALLBACK_MARKER

logger = logging.getLogger(__name__)


class MediaCrawlerError(RuntimeError):
    pass


class QueueTimeout(MediaCrawlerError):
    """Another crawl kept the machine-wide lock for longer than the queue timeout."""


# --- queue ------------------------------------------------------------------

_LOCK_NAME = "crawl.lock"
_QUEUE_POLL_SECONDS = 0.5
# SIGTERM to the crawler, then this long for it to close its browser tab before SIGKILL.
_STOP_GRACE_SECONDS = 20


@dataclass
class CrawlSlot:
    """Proof of holding the crawl lock, and how long it took to get."""

    lock_file: IO[str]
    waited_seconds: float


def _describe_holder(lock_file: IO[str]) -> str:
    try:
        lock_file.seek(0)
        holder = json.loads(lock_file.read())
        running = int(time.time() - holder["since"])
        return f"{holder['label']} (pid {holder['pid']}, running for {running}s)"
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


@contextmanager
def crawl_lock(settings: Settings, label: str) -> Iterator[CrawlSlot]:
    """Wait for the machine-wide crawl lock and hold it for the block.

    flock is released when every holder of the open file is gone, so a dead process
    can never leave it stuck, and each call opens its own file, so it also queues
    calls within one process.
    """
    path = settings.guard_dir() / _LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    timeout = settings.queue_timeout_seconds
    started = time.monotonic()
    with open(path, "a+", encoding="utf-8") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - started >= timeout:
                    raise QueueTimeout(
                        f"Waited {timeout}s for another crawl to finish. "
                        f"Holding the queue: {_describe_holder(lock_file)}. "
                        "Retry later; if that process is stuck, it is the one to stop."
                    ) from None
                time.sleep(_QUEUE_POLL_SECONDS)
        lock_file.seek(0)
        lock_file.truncate()
        json.dump({"label": label, "pid": os.getpid(), "since": time.time()}, lock_file)
        lock_file.flush()
        yield CrawlSlot(lock_file, time.monotonic() - started)


def _build_command(settings: Settings, platform: str, crawl_type: str) -> list[str]:
    base: list[str]
    if settings.python_runner == "uv":
        base = ["uv", "run", "main.py"]
    else:
        base = [settings.python_runner, "main.py"]
    base += [
        "--platform", platform,
        "--lt", settings.login_type,
        "--type", crawl_type,
        "--save_data_option", settings.save_data_option,
    ]
    return base


# --- config patching --------------------------------------------------------

_CONFIG_REL = Path("config") / "base_config.py"
_SNAPSHOT_NAME = "base_config.py.running"


def _set_py_assignment(source: str, name: str, value_literal: str) -> str:
    """Replace a top-level ``NAME = ...`` assignment; append if missing."""
    pattern = re.compile(rf"^{re.escape(name)}\s*=.*$", re.MULTILINE)
    replacement = f"{name} = {value_literal}"
    if pattern.search(source):
        return pattern.sub(replacement, source, count=1)
    return source.rstrip() + f"\n{replacement}\n"


def _py_list(values: list[str]) -> str:
    inner = ", ".join(repr(v) for v in values)
    return f"[{inner}]"


def build_config_patch(
    settings: Settings,
    *,
    keywords: str | None,
    specified_ids: list[str] | None,
    creator_ids: list[str] | None,
    max_count: int,
    with_comments: bool,
) -> dict[str, str]:
    """Return a mapping of config var name -> python literal to inject.

    Uses the most common MediaCrawler config variable names. Unknown names are
    simply appended and ignored by MediaCrawler, so this stays safe.
    """
    patch: dict[str, str] = {
        "CRAWLER_MAX_NOTES_COUNT": str(max_count),
        "ENABLE_GET_COMMENTS": str(with_comments),
        "ENABLE_CDP_MODE": str(settings.enable_cdp),
        "ENABLE_IP_PROXY": str(settings.enable_proxy),
        "CRAWLER_MAX_SLEEP_SEC": str(int(max(1, settings.delay_max_seconds))),
    }
    if keywords is not None:
        patch["KEYWORDS"] = repr(keywords)
    if specified_ids is not None:
        # MediaCrawler uses different names per platform; set the common ones.
        # Xiaohongshu detail mode reads full note URLs (they carry the xsec_token).
        patch["XHS_SPECIFIED_NOTE_URL_LIST"] = _py_list(specified_ids)
        patch["XHS_SPECIFIED_ID_LIST"] = _py_list(specified_ids)
        patch["DY_SPECIFIED_ID_LIST"] = _py_list(specified_ids)
        patch["WEIBO_SPECIFIED_ID_LIST"] = _py_list(specified_ids)
        patch["BILI_SPECIFIED_ID_LIST"] = _py_list(specified_ids)
        patch["KS_SPECIFIED_ID_LIST"] = _py_list(specified_ids)
        patch["TIEBA_SPECIFIED_ID_LIST"] = _py_list(specified_ids)
        patch["ZHIHU_SPECIFIED_ID_LIST"] = _py_list(specified_ids)
    if creator_ids is not None:
        patch["XHS_CREATOR_ID_LIST"] = _py_list(creator_ids)
        patch["DY_CREATOR_ID_LIST"] = _py_list(creator_ids)
        patch["WEIBO_CREATOR_ID_LIST"] = _py_list(creator_ids)
        patch["BILI_CREATOR_ID_LIST"] = _py_list(creator_ids)
        patch["KS_CREATOR_ID_LIST"] = _py_list(creator_ids)
        patch["TIEBA_CREATOR_URL_LIST"] = _py_list(creator_ids)
        patch["ZHIHU_CREATOR_URL_LIST"] = _py_list(creator_ids)
    return patch


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` in one step, so nothing ever reads it half-written."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


@contextmanager
def patched_config(settings: Settings, patch: dict[str, str]) -> Iterator[None]:
    """Temporarily patch base_config.py, restoring the original bytes on exit.

    Call this while holding the crawl lock. The original is also kept on disk for the
    length of the run: a server killed mid-run never reaches ``finally``, so a snapshot
    found here means the last holder died and the file still has its patch.
    """
    config_path = settings.mediacrawler_home / _CONFIG_REL
    if not config_path.exists():
        raise MediaCrawlerError(
            f"Cannot find MediaCrawler config at {config_path}. "
            "Check MEDIACRAWLER_HOME."
        )
    snapshot = settings.guard_dir() / _SNAPSHOT_NAME
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.exists():
        logger.warning("A crawl died mid-run; restoring %s from %s", config_path, snapshot)
        _write_atomic(config_path, snapshot.read_bytes())
    original = config_path.read_bytes()
    _write_atomic(snapshot, original)
    try:
        patched = original.decode("utf-8")
        for name, literal in patch.items():
            patched = _set_py_assignment(patched, name, literal)
        _write_atomic(config_path, patched.encode("utf-8"))
        yield
    finally:
        _write_atomic(config_path, original)
        snapshot.unlink(missing_ok=True)


# --- run --------------------------------------------------------------------


def _stop_process_group(proc: subprocess.Popen) -> None:
    """Stop the crawler and everything it started.

    ``uv run`` keeps Python as a child, so killing only ``proc`` would leave the crawl running.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=_STOP_GRACE_SECONDS)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _read_log(log: IO[bytes]) -> str:
    log.seek(0)
    return log.read().decode("utf-8", "replace")


def run_crawl(
    settings: Settings,
    *,
    slot: CrawlSlot,
    platform: str,
    crawl_type: str,
    keywords: str | None = None,
    specified_ids: list[str] | None = None,
    creator_ids: list[str] | None = None,
    max_count: int = 20,
    with_comments: bool = True,
) -> dict[str, Any]:
    """Patch config, run MediaCrawler once, return run metadata.

    ``slot`` is the held crawl lock, which the crawler inherits.

    Returns a dict with keys: started_ts, returncode, stdout_tail, stderr_tail,
    timed_out, notices. ``notices`` keeps log lines that matter even when they
    scroll out of the tails. The caller reads the ``data/`` directory for
    actual results.
    """
    cmd = _build_command(settings, platform, crawl_type)
    patch = build_config_patch(
        settings,
        keywords=keywords,
        specified_ids=specified_ids,
        creator_ids=creator_ids,
        max_count=max_count,
        with_comments=with_comments,
    )

    started_ts = time.time()
    timed_out = False
    returncode = -1

    with patched_config(settings, patch), tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(settings.mediacrawler_home),
                stdout=out,
                stderr=err,
                pass_fds=(slot.lock_file.fileno(),),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise MediaCrawlerError(
                f"Failed to launch MediaCrawler runner {cmd[0]!r}: {exc}. "
                "Is 'uv' (or your configured MEDIACRAWLER_RUNNER) installed and on PATH?"
            ) from exc
        try:
            returncode = proc.wait(timeout=settings.run_timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_process_group(proc)
        except BaseException:
            _stop_process_group(proc)
            raise
        stdout_text = _read_log(out)
        stderr_text = _read_log(err)

    log_lines = f"{stdout_text}\n{stderr_text}".splitlines()
    return {
        "command": " ".join(cmd),
        "started_ts": started_ts,
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout_tail": stdout_text[-2000:],
        "stderr_tail": stderr_text[-2000:],
        "notices": [
            line.strip()[-300:]
            for marker in (ANON_ONLY_MARKER, CDP_FALLBACK_MARKER)
            for line in [ln for ln in log_lines if marker in ln][:1]
        ],
    }


def probe_help(settings: Settings) -> tuple[bool, str]:
    """Return whether ``main.py --help`` succeeded and its output, for diagnostics."""
    if settings.python_runner == "uv":
        cmd = ["uv", "run", "main.py", "--help"]
    else:
        cmd = [settings.python_runner, "main.py", "--help"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(settings.mediacrawler_home),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001 - diagnostic best-effort
        return False, f"<failed to run --help: {exc}>"
