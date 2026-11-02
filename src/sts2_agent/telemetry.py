"""Public experiment journals and a Windows single-writer process lock."""

from datetime import datetime, timezone
import json
import msvcrt
import os
from pathlib import Path
import threading
import time
import warnings


# These records must reach disk before a controller can forget or submit an action.
DURABLE_STREAMS = frozenset({"transitions", "execution-requests", "execution-receipts", "decisions"})
# Windows readers can briefly deny replacement; keep the previous complete ledger until retry succeeds.
REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)
# Version two records observation capabilities and derived public combat evidence.
TRANSITION_SCHEMA_VERSION = 2


def utc_now() -> str:
    """Return a portable event timestamp."""
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict) -> None:
    """Replace a small local JSON document after a complete temporary write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(len(REPLACE_RETRY_DELAYS) + 1):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == len(REPLACE_RETRY_DELAYS):
                raise
            time.sleep(REPLACE_RETRY_DELAYS[attempt])


def read_jsonl(path: Path, issues: list | None = None):
    """Stream valid objects while reporting corrupt or incomplete records with their line numbers."""
    if not path.exists():
        return
    with path.open("rb") as handle:
        for number, raw in enumerate(handle, 1):
            try:
                value = json.loads(raw.decode("utf-8"))
                if not isinstance(value, dict) or not raw.endswith(b"\n"):
                    raise ValueError
            except (ValueError, UnicodeError):
                issue = {"file": path.name, "line": number, "error": "invalid_or_incomplete_jsonl_record"}
                if issues is None:
                    warnings.warn(f"{path.name}:{number}: invalid or incomplete JSONL record", RuntimeWarning)
                else:
                    issues.append(issue)
                continue
            yield value


class Journal:
    """Keep only public observations, selected actions, metadata, and safe receipts."""

    def __init__(self, directory: Path, *, resume: bool = False):
        """Create one immutable identity directory for a smoke segment."""
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=resume)
        self._lock = threading.RLock()
        self._opened = set()

    def event(self, stream: str, payload: dict) -> None:
        """Append one immediately flushed correlated public event."""
        path = self.directory / (stream + ".jsonl")
        record = (json.dumps({"timestamp_utc": utc_now(), **payload},
                             ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        with self._lock:
            if stream not in self._opened:
                if path.exists() and path.stat().st_size:
                    with path.open("rb+") as existing:
                        existing.seek(-1, 2)
                        if existing.read(1) != b"\n":
                            # Preserve the damaged evidence, but never concatenate it with the next event.
                            existing.write(b"\n")
                            warnings.warn(f"{path.name}: preserved an unterminated record before resuming", RuntimeWarning)
                self._opened.add(stream)
            with path.open("ab") as handle:
                handle.write(record)
                handle.flush()
                if stream in DURABLE_STREAMS:
                    os.fsync(handle.fileno())

    def document(self, name: str, payload: dict) -> None:
        """Write a run manifest or final result."""
        write_json(self.directory / name, payload)


class WriterLock:
    """Prevent two external controller processes from acting on the game."""

    def __init__(self, path: Path):
        """Reserve one Windows byte lock for the lifetime of the context."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")

    def __enter__(self):
        """Fail visibly if another controller owns the writer lock."""
        try:
            self.handle.seek(0)
            self.handle.write(b"0")
            self.handle.flush()
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.handle.close()
            raise RuntimeError("Another external controller holds the writer lock.") from None
        return self

    def __exit__(self, *_args):
        """Release the OS lock even when a run pauses or fails."""
        self.handle.seek(0)
        msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.handle.close()
