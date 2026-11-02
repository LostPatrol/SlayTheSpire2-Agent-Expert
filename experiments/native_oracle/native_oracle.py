"""Run synthetic native-engine fixtures without loading the real game's saves or RNG."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
from queue import Queue, Empty
import subprocess
import threading

# The downloaded code and proprietary local DLLs remain outside publishable experiment sources.
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHECKOUT = ROOT / "agent/codex/priority-fixes-20261029/native/sts2-cli"
DOTNET = ROOT / ".tools/dotnet/dotnet.exe"
# A stalled headless action must never make a batch wait indefinitely.
RESPONSE_TIMEOUT = 20


class NativeOracle:
    """One isolated native process; each init starts a fresh synthetic encounter."""

    def __init__(self, checkout: str | Path = DEFAULT_CHECKOUT, *, runtime_dir=None):
        """Select an already prepared local checkout and process-output directory."""
        self.checkout = Path(checkout).resolve()
        self.runtime_dir = Path(runtime_dir or self.checkout / "runtime").resolve()
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.process = None
        self.lines = None
        self.stderr = deque(maxlen=200)

    def _start(self):
        """Launch the copied native DLL and explicit oracle-only adapter mode."""
        self.close()
        dll = self.checkout / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll"
        if not dll.exists():
            raise RuntimeError("Run experiments/native_oracle/setup.py before using the native oracle")
        env = os.environ.copy()
        env.update(STS2_LIB=str(self.checkout / "lib"), STS2_ORACLE="1")
        # Never allow inherited game-path resolution to bind a second installed engine.
        env.pop("STS2_GAME_DIR", None)
        self.process = subprocess.Popen(
            [str(DOTNET), str(dll)], cwd=self.runtime_dir, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.lines = Queue()
        self.stderr.clear()
        process, lines, errors = self.process, self.lines, self.stderr
        threading.Thread(target=self._stdout, args=(process, lines), daemon=True).start()
        threading.Thread(target=self._stderr, args=(process, errors), daemon=True).start()
        ready = self._receive()
        if ready.get("type") != "ready":
            raise RuntimeError(f"Native oracle did not become ready: {ready}")

    @staticmethod
    def _stdout(process, lines):
        """Queue complete JSONL responses without blocking a request's deadline."""
        for line in process.stdout:
            if line.startswith("{"):
                lines.put(line)
        lines.put(None)

    @staticmethod
    def _stderr(process, errors):
        """Drain native diagnostics to avoid pipe backpressure."""
        for line in process.stderr:
            errors.append(line.rstrip())

    def _receive(self):
        """Fail explicitly on a native crash or stall, preserving diagnostics."""
        try:
            line = self.lines.get(timeout=RESPONSE_TIMEOUT)
        except Empty:
            self.close()
            raise RuntimeError("native_oracle_timeout") from None
        if line is None:
            raise RuntimeError("native_oracle_exited: " + "\n".join(self.stderr)[-3000:])
        return json.loads(line)

    def request(self, command: dict) -> dict:
        """Send only test-process commands; this class has no live-bridge connection."""
        self.process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        result = self._receive()
        if result.get("type") == "error":
            raise RuntimeError(result.get("message", "native_oracle_error"))
        return result

    def init(self, setup: dict) -> dict:
        """Create a synthetic run, encounter and exact in-memory pile fixture."""
        self._start()
        self.request({"cmd": "start_run", "character": setup.get("character", "Ironclad"),
                      "ascension": 0, "seed": setup.get("seed", "synthetic-parity-v1")})
        cards = [card for pile in ("hand", "draw", "discard", "exhaust") for card in setup.get(pile, [])]
        deck = [card if isinstance(card, str) else card["card_id"] for card in cards]
        self.request({"cmd": "set_player", "relics": setup.get("relics", []),
                      "deck": setup.get("deck", deck),
                      "potions": setup.get("potions", []),
                      "hp": setup.get("hp", 80), "max_hp": setup.get("max_hp", 80)})
        self.request({"cmd": "enter_room", "type": "combat",
                      "encounter": setup.get("encounter", "NIBBITS_WEAK")})
        return self.request({"cmd": "oracle_setup", "setup": setup})

    def step(self, action: dict) -> dict:
        """Apply a native player action or explicit choice and return its observed successor."""
        if action.get("type") == "end_turn":
            raise RuntimeError("Native end-turn adapter fallbacks are not validated as oracle evidence")
        return self.request({"cmd": "oracle_step", "action": action})

    def close(self):
        """Reap only this class's child, leaving all installed-game processes alone."""
        if self.process is None:
            return
        process, self.process = self.process, None
        if process.poll() is None:
            try:
                process.stdin.write('{"cmd":"quit"}\n')
                process.stdin.flush()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def __enter__(self):
        """Allow deterministic process cleanup in a batch context."""
        return self

    def __exit__(self, *_):
        """Close the local test child even when a fixture fails."""
        self.close()


def run_jsonl():
    """Serve one synthetic fixture per input line; no real saves are accepted."""
    import sys
    with NativeOracle() as oracle:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                initial = oracle.init(request["setup"])
                steps = [oracle.step(action) for action in request.get("actions", [])]
                result = {"ok": True, "id": request.get("id"), "initial": initial, "steps": steps}
            except Exception as error:
                result = {"ok": False, "error": str(error)}
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    run_jsonl()
