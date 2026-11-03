"""Prepare the native game and rotate finished ledgers for explicit dashboard launches."""

from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
import threading
from uuid import uuid4

from .bridge import Bridge, BridgeError
from .session import DashboardSession, SessionError, _archive_terminal_ledger
from .public import configured_start, MAX_START_ASCENSION
from .save_guard import SaveGuardError, ensure_full_unlock
from .telemetry import WriterLock, write_json
from .presentation import (PREFERENCE_FILE, language_from_config, presentation_run_active,
                           saved_language)
import json


# The existing native launcher can spend up to 90 attempts waiting for each bridge endpoint.
GAME_START_TIMEOUT = 240
# Keep only recent launch receipts; a running launch is also protected independently of its ID.
LAUNCH_RECEIPTS = 20
# These native character IDs are the user-selectable launch policy.
LAUNCH_CHARACTERS = (("IRONCLAD", "铁甲战士"), ("SILENT", "静默猎手"),
                     ("REGENT", "储君"), ("NECROBINDER", "亡灵契约师"), ("DEFECT", "故障机器人"))


def ensure_launch_save(root: Path) -> dict:
    """Refuse to open the game unless the authorized full-unlock profile is active."""
    try:
        return ensure_full_unlock(root)
    except SaveGuardError as error:
        raise SessionError(str(error)) from None


def _open_game(config: dict, root: Path) -> dict:
    """Reuse a healthy native process or invoke the project's existing launch script once."""
    ensure_launch_save(root)
    bridge = Bridge(config)
    try:
        try:
            bridge.health()
        except BridgeError as error:
            if error.code != "bridge_unreachable":
                raise
            powershell = shutil.which("pwsh")
            if powershell is None:
                bundled = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
                if bundled.is_file():
                    powershell = str(bundled)
            if powershell is None:
                raise SessionError("启动游戏需要安装 PowerShell 7（pwsh）。")
            try:
                # The long-lived game inherits these handles; PIPE would wait for it to exit.
                result = subprocess.run(
                    [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(root / "scripts/start-game.ps1")], cwd=root,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=GAME_START_TIMEOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise SessionError("游戏启动未完成，请检查游戏窗口、安装路径和 Mod，然后重试。") from None
            if result.returncode:
                raise SessionError("游戏启动失败，请检查安装路径；已有游戏时请确认其 Mod 和桥服务已启用。")
            bridge.health()
        public, _ = bridge.settled()
        return public
    finally:
        bridge.client.close()


class GameLauncher:
    """Serialize explicit launch preparation while retaining the existing sole policy worker."""

    def __init__(self, config: dict, root: Path, session=None):
        """Keep launch progress separate from the durable policy ledger and budget."""
        self.config = config
        self.root = Path(root).resolve()
        self.session = session
        self.lock = threading.RLock()
        self.thread = None
        self.closing = threading.Event()
        self.state = {"status": "idle", "message": "点击启动游戏，再开始自动游玩。"}
        self.receipts = {}
        self._settings_file = self.root / "local/launch-settings.json"
        self._settings_source = "default"
        self._presentation_file = self.root / PREFERENCE_FILE
        self.config.setdefault("presentation", {})["language"] = saved_language(
            self.root, language_from_config(self.config))
        if self._settings_file.is_file():
            try:
                saved = configured_start(json.loads(self._settings_file.read_text(encoding="utf-8")))
                self.config.setdefault("policy", {}).update(saved)
                self._settings_source = "preference"
            except (OSError, ValueError, RuntimeError):
                # An invalid optional preference must not prevent ordinary startup.
                pass

    def snapshot(self) -> dict:
        """Combine the current public session with lightweight launch progress."""
        with self.lock:
            return {**self.session.snapshot(), "game_launch": deepcopy(self.state),
                    "launch_settings": self.launch_settings(),
                    "presentation_settings": self.presentation_settings()}

    def presentation_settings(self) -> dict:
        """Expose current-run language or the selected next-run preference without game effects."""
        view = self.session.snapshot() if self.session is not None else {}
        active = presentation_run_active(view)
        busy = (self.state["status"] == "starting" or self.closing.is_set()
                or view.get("desired_state", "paused") != "paused" or bool(view.get("inflight"))
                or bool(view.get("request_pending")) or bool(view.get("decision_pending"))
                or (self.root / "local/pending-action.json").exists())
        current = view.get("presentation_language", language_from_config(
            getattr(self.session, "config", self.config)))
        selected = language_from_config(self.config) if view.get("terminal_evidence") else current
        return {"language": selected, "current_language": current, "editable": not active and not busy,
                "reason": "Presentation language is fixed for the current run." if active else (
                    "Pause and wait for all pending operations." if busy else None)}

    def set_presentation_settings(self, language: str) -> dict:
        """Persist an explicit pre-run preference while keeping completed ledgers unchanged."""
        with self.lock:
            if not self.presentation_settings()["editable"]:
                raise SessionError("Choose the presentation language before a new run while paused.")
            self.session.configure_presentation(language)
            write_json(self._presentation_file, {"language": language})
            self.config.setdefault("presentation", {})["language"] = language
            return self.presentation_settings()

    def launch_settings(self) -> dict:
        """Expose the requested setup and a live edit lock without touching game controls."""
        view = self.session.snapshot() if self.session is not None else {}
        public = (view.get("observation") or {}).get("state", {})
        active = public.get("screen") not in {None, "MAIN_MENU", "CHARACTER_SELECT"} and not view.get("terminal_evidence")
        busy = (self.state["status"] == "starting" or self.closing.is_set()
                or view.get("desired_state", "paused") != "paused"
                or bool(view.get("inflight")) or bool(view.get("request_pending")))
        policy = getattr(self.session, "config", self.config).get("policy", {})
        target = configured_start(policy)
        return {**target, "editable": not active and not busy,
                "choices": [{"id": ident, "label": label} for ident, label in LAUNCH_CHARACTERS],
                "max_ascension": MAX_START_ASCENSION, "default": {"character": "IRONCLAD", "ascension": 0},
                "source": "active_run" if active else self._settings_source,
                "reason": "进行中的对局已固定角色与难度。" if active else (
                    "请先暂停并等待当前操作完成。" if busy else None)}

    def set_launch_settings(self, settings: dict) -> dict:
        """Persist an explicit pre-run selection without injecting strategic guidance."""
        with self.lock:
            if not self.launch_settings()["editable"]:
                raise SessionError("请在新局开始前暂停设置；进行中的对局不可更改。")
            target = configured_start(settings)
            self.session.configure_launch(target)
            write_json(self._settings_file, target)
            # A terminal ledger is archived during preparation; its successor uses this config.
            self.config.setdefault("policy", {}).update(target)
            self._settings_source = "preference"
            return self.launch_settings()

    def require_ready(self):
        """Keep policy mutations out of the brief session replacement and native loading stage."""
        if self.state["status"] == "starting" or self.closing.is_set():
            raise SessionError("游戏正在准备，请等待启动完成后再操作。")
        return self.session

    def start(self, command_id: str) -> dict:
        """Acknowledge once and prepare asynchronously without calling the model."""
        with self.lock:
            if command_id in self.receipts:
                return deepcopy(self.receipts[command_id])
            if self.state["status"] == "starting":
                raise SessionError("游戏启动请求已在处理，请等待完成。")
            view = self.session.snapshot()
            if (view.get("inflight") or view.get("request_pending")
                    or (self.root / "local/pending-action.json").exists()):
                raise SessionError("还有待核对的动作或模型请求，请等待其完成。")
            if view.get("desired_state", "paused") != "paused":
                raise SessionError("请先暂停自动游玩，再启动或准备游戏。")
            self.state = {"status": "starting", "message": "正在启动游戏并准备会话，已结束的旧账本会自动归档。"}
            receipt = {"command_id": command_id, "status": "accepted", "message": self.state["message"]}
            self.receipts[command_id] = receipt
            if len(self.receipts) > LAUNCH_RECEIPTS:
                self.receipts.pop(next(iter(self.receipts)))
            self.thread = threading.Thread(target=self._prepare, name="dashboard-game-launch", daemon=True)
            self.thread.start()
            return deepcopy(receipt)

    def _replace_session(self) -> DashboardSession:
        """Restore the current ledger or construct a fresh run after a completed archive."""
        current = DashboardSession(self.config, self.root)
        with self.lock:
            self.session = current
        return current

    def _prepare(self) -> None:
        """Stop observation during native navigation, then restart one paused policy worker."""
        previous = self.session
        current = None
        try:
            previous.shutdown()
            if getattr(previous, "_thread", None) and previous._thread.is_alive():
                raise SessionError("控制器尚未停止，请稍后重试；当前账本已保留。")
            # The dashboard lifetime owns its lock; no other controller may navigate or rotate here.
            with WriterLock(self.root / "local/controller.lock"):
                public = _open_game(self.config, self.root)
                terminal = previous.snapshot().get("terminal_evidence")
                if terminal and public.get("screen") != "GAME_OVER":
                    _archive_terminal_ledger(self.root)
                current = self._replace_session()
                current.prepare_launch_connection()
                obs, _ = current._observe(settled=True)
                if current._record_game_over(obs):
                    if not current.navigate_menu("return_to_main_menu"):
                        raise SessionError("终局结算尚未完成，请等待原生返回主菜单按钮可用后再试。")
                    current.shutdown()
                    _archive_terminal_ledger(self.root)
                    current = None
                    current = self._replace_session()
                current.navigate_menu("continue_run")
                obs, _ = current._observe(settled=True)
                if current._record_game_over(obs):
                    raise SessionError("存档仍停留在终局，结果已保留；请完成游戏原生结算后再试。")
                # A new ID is needed when preparing the same unfinished run more than once.
                current.command(command_id="launch-ready-" + str(uuid4()), command="start")
            if self.closing.is_set():
                return
            current.start_background()
            with self.lock:
                recording_ready = obs["state"].get("observation_schema_version", 0) >= 2
                message = "游戏已就绪，点击开始自动游玩。"
                if not recording_ready:
                    message += " 当前游戏尚未加载完整复盘日志功能，请关闭游戏后从这里重新启动。"
                self.state = {"status": "ready", "message": message,
                              "complete_public_recording_available": recording_ready}
        except (OSError, ValueError, RuntimeError) as error:
            # Rebuild only after the previous worker has exited; budgets and pending receipts remain intact.
            if current is None and not (getattr(previous, "_thread", None) and previous._thread.is_alive()):
                try:
                    current = self._replace_session()
                except (OSError, ValueError, RuntimeError):
                    current = None
            if current is not None and not self.closing.is_set():
                try:
                    current.start_background()
                except (OSError, ValueError, RuntimeError):
                    pass
            message = str(error) if isinstance(error, SessionError) else "游戏准备失败，当前账本已保留，请检查游戏桥连接后重试。"
            with self.lock:
                self.state = {"status": "error", "message": message}
        finally:
            if self.closing.is_set() and current is not None:
                current.shutdown()

    def shutdown(self) -> None:
        """Close admission and let any outstanding native launch finish its ledger cleanup."""
        self.closing.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                return
        self.session.shutdown()
