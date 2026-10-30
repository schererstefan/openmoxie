"""Read-only bounded tail reader for the server / MQTT broker logs.

The running server container mounts ./local/work to /app/site/work, which is
exactly ``settings.DATA_STORE_DIR`` (BASE_DIR / 'work') in this project, so
both the Django server log (debug.log, including device logs forwarded as
logger.debug) and the broker log (mosquitto.log) are readable from the hive
app with no new mount and no live-install path.

Security contract:
- Only the two fixed filenames in ALLOWED_LOG_FILES may ever be read. Any
  other filename (user-supplied paths, traversal, absolute paths) is
  rejected with ValueError before any filesystem access.
- Files are read from the end in bounded blocks; a whole file is never
  loaded into memory, and at most MAX_TAIL_BYTES are ever scanned.
- A missing/unreadable file yields an empty result, never an exception.
This module only reads; it writes nothing and touches no DB or MQTT state.
"""
import re
from pathlib import Path

# Fixed filename -> display label. This dict is the complete allowlist.
ALLOWED_LOG_FILES = {
    "debug.log": "Server log (debug.log)",
    "mosquitto.log": "MQTT broker log (mosquitto.log)",
}

DEFAULT_TAIL_LINES = 200
MAX_TAIL_LINES = 2000
MAX_TAIL_BYTES = 4 * 1024 * 1024
_BLOCK_SIZE = 8192

FAILURE_MARKERS = (
    "timeout", "404", "checksum", "signature", "corrupt", "failed", "unknown ca",
)
_OTA_WORD = re.compile(r"\bota\b", re.IGNORECASE)


def default_work_dir():
    """The hive app work dir (container: /app/site/work), never a live path."""
    try:
        from django.conf import settings

        if settings.configured:
            data_store = getattr(settings, "DATA_STORE_DIR", None)
            if data_store:
                return Path(data_store)
            base_dir = getattr(settings, "BASE_DIR", None)
            if base_dir:
                return Path(base_dir) / "work"
    except Exception:
        pass
    # site/hive/logtail.py -> site/work, same mapping as BASE_DIR / 'work'.
    return Path(__file__).resolve().parent.parent / "work"


def resolve_log_path(filename, work_dir=None):
    """Resolve one allowlisted filename inside the work dir, else ValueError."""
    if not isinstance(filename, str) or filename not in ALLOWED_LOG_FILES:
        raise ValueError(f"log file not allowed: {filename!r}")
    base = Path(work_dir) if work_dir is not None else default_work_dir()
    path = base / filename
    # Defence in depth: the allowlisted names contain no separators, but
    # refuse anything whose resolved parent escapes the work dir.
    if path.resolve().parent != base.resolve():
        raise ValueError(f"log file not allowed: {filename!r}")
    return path


def parse_line_count(value, default=DEFAULT_TAIL_LINES):
    """Parse a requested line count, clamped to [1, MAX_TAIL_LINES]."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(count, MAX_TAIL_LINES))


def read_last_lines(path, count):
    """Return at most ``count`` last lines of ``path``, reading from the end.

    Reads backwards in blocks until enough newlines are seen or
    MAX_TAIL_BYTES have been scanned, so memory stays bounded regardless
    of file size. Missing/unreadable files return [].
    """
    if count <= 0:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            remaining = fh.tell()
            if remaining == 0:
                return []
            blocks = []
            scanned = 0
            newlines = 0
            while remaining > 0 and newlines <= count and scanned < MAX_TAIL_BYTES:
                step = min(_BLOCK_SIZE, remaining, MAX_TAIL_BYTES - scanned)
                remaining -= step
                fh.seek(remaining)
                block = fh.read(step)
                blocks.append(block)
                scanned += len(block)
                newlines += block.count(b"\n")
            truncated = remaining > 0
    except OSError:
        return []
    text = b"".join(reversed(blocks)).decode("utf-8", errors="replace")
    lines = text.splitlines()
    if truncated and lines:
        lines = lines[1:]  # first segment may start mid-line
    return lines[-count:]


def line_matches(line, device="", tag="", level="", q=""):
    """True if every supplied filter appears in the line (case-insensitive).

    Device ids, tags (e.g. BoUpdater) and levels (ERROR, DEBUG, ...) all
    appear as text in both log formats, so one substring rule covers them.
    """
    lowered = line.lower()
    for needle in (device, tag, level, q):
        if needle and needle.lower() not in lowered:
            return False
    return True


def classify_line(line):
    """Annotate a line for OTA / failure highlighting."""
    lowered = line.lower()
    return {
        "text": line,
        "ota": "boupdater" in lowered or bool(_OTA_WORD.search(line)),
        "failure": any(marker in lowered for marker in FAILURE_MARKERS),
    }


def get_log_tail(filename, count=DEFAULT_TAIL_LINES, device="", tag="",
                 level="", q="", work_dir=None):
    """Tail one allowlisted log file, filtered within the tailed window.

    At most ``count`` lines are read and at most ``count`` are returned;
    filters apply inside that window (raise the line count to look
    further back). Raises ValueError for any non-allowlisted filename.
    """
    path = resolve_log_path(filename, work_dir=work_dir)
    count = parse_line_count(count)
    exists = path.is_file()
    lines = []
    if exists:
        lines = [
            classify_line(line)
            for line in read_last_lines(path, count)
            if line_matches(line, device=device, tag=tag, level=level, q=q)
        ][-count:]
    return {
        "filename": filename,
        "label": ALLOWED_LOG_FILES[filename],
        "exists": exists,
        "lines": lines,
        "count": len(lines),
    }


def collect_logs(filenames=None, count=DEFAULT_TAIL_LINES, device="", tag="",
                 level="", q="", work_dir=None):
    """Tail the requested allowlisted files (default: both), keyed by name."""
    if filenames is None:
        filenames = list(ALLOWED_LOG_FILES)
    return {
        filename: get_log_tail(filename, count=count, device=device, tag=tag,
                               level=level, q=q, work_dir=work_dir)
        for filename in filenames
    }
