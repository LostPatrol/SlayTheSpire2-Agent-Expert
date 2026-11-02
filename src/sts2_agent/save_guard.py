"""Keep every dashboard and native launch on the authorized full-unlock profile."""
# Full-unlock main profile guard. Every dashboard start and every native launch verifies the
# active Steam profile against the frozen 2026-10-29 "unlock all" baseline; a deviating profile is
# replaced only while the game is closed, after the deviating bytes are backed up. The guard exists
# because the launch path alone cannot prove unlock state: the bridge only exposes public run data.

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import shutil
import subprocess

# Frozen native "unlock all" progress kept inside the project; restored bytes come only from here.
BASELINE = Path("local/saves/full-unlock/progress.save")
# Replaced profiles are copied here before the baseline is written.
BACKUP_ROOT = Path("local/game-saves-backup")
# Machine configuration that selects the Steam client used for every launch.
MACHINE = Path("configs/machine.json")
# Modded saves are the only progress the dashboard launches load.
MODDED = Path("modded")
# Full-unlock maxima of the authorized baseline; counters only grow during ordinary play.
FULL_UNLOCK_COUNTERS = {"epochs": 57, "cards": 578, "relics": 297,
                        "potions": 65, "events": 62, "acts": 4, "characters": 5}
# Epoch states at or beyond "revealed"; every other state still locks timeline content.
UNLOCKED_EPOCH_STATES = frozenset({"revealed", "obtained"})
# Native process that must be closed before a restore may replace the profile.
GAME_PROCESS = "SlayTheSpire2.exe"
# Replaced together so the game's own backup cannot resurrect a partial profile.
SAVE_FILES = ("progress.save", "progress.save.backup")


class SaveGuardError(RuntimeError):
    """Raised when the authorized full-unlock profile cannot be verified or restored."""


def _timestamp() -> str:
    """Return a sortable local timestamp for backup directories."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    """Return the lowercase SHA256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def game_running() -> bool | None:
    """Report whether the native game is alive; None means the check is unavailable."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {GAME_PROCESS}", "/NH"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return GAME_PROCESS.lower() in (result.stdout or "").lower()


def inspect_progress(path: Path) -> dict:
    """Count the profile counters that separate a full-unlock save from a fresh one."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    epochs = list(data.get("epochs") or [])
    return {
        "epochs": len(epochs),
        "locked_epochs": sum(1 for epoch in epochs
                             if epoch.get("state") not in UNLOCKED_EPOCH_STATES),
        "cards": len(data.get("discovered_cards") or []),
        "relics": len(data.get("discovered_relics") or []),
        "potions": len(data.get("discovered_potions") or []),
        "events": len(data.get("discovered_events") or []),
        "acts": len(data.get("discovered_acts") or []),
        "characters": len(data.get("character_stats") or []),
    }


def is_full_unlock(counters: dict) -> bool:
    """Require no locked epoch and every discovered counter at its full-unlock size."""
    if counters["locked_epochs"]:
        return False
    return all(counters[name] >= minimum for name, minimum in FULL_UNLOCK_COUNTERS.items())


def _client_dir(root: Path, client_id: str | None = None) -> Path | None:
    """Resolve a Steam client directory, or None without a machine config."""
    client = str(client_id) if client_id is not None else None
    if client is None:
        try:
            client = json.loads((root / MACHINE).read_text(encoding="utf-8"))["client_id"]
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError) as error:
            raise SaveGuardError(f"机器配置不可读：{error}") from None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SaveGuardError("无法定位 APPDATA，不能校验游戏存档。")
    return Path(appdata) / "SlayTheSpire2/default" / str(client)


def _active_profile(client_dir: Path) -> int:
    """Read the profile the next native launch will load; profile 1 is the documented default."""
    try:
        selected = json.loads((client_dir / "profile.save").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1
    value = selected.get("last_profile_id")
    if type(value) is int and value > 0:
        return value
    return 1


def saves_dir(root: Path, client_id: str | None = None) -> Path | None:
    """Resolve the save directory of the profile the next native launch will load."""
    client_dir = _client_dir(root, client_id)
    if client_dir is None:
        return None
    return client_dir / MODDED / f"profile{_active_profile(client_dir)}" / "saves"


def _mismatch(counters: dict | None, progress: Path) -> str:
    """Describe why a profile cannot be launched without restoring the baseline."""
    if counters is None:
        return f"未找到主档进度文件：{progress}"
    return (f"当前主档不是全解锁存档：epoch {counters['epochs']}/57、"
            f"锁定 {counters['locked_epochs']}、卡牌 {counters['cards']}/578、"
            f"遗物 {counters['relics']}/297、药水 {counters['potions']}/65、"
            f"事件 {counters['events']}/62、幕 {counters['acts']}/4、"
            f"角色 {counters['characters']}/5。")


def _backup(save_dir: Path, root: Path) -> Path | None:
    """Copy the deviating profile aside before it is replaced; returns the backup directory."""
    existing = [save_dir / name for name in SAVE_FILES if (save_dir / name).is_file()]
    if not existing:
        return None
    target = root / BACKUP_ROOT / f"{_timestamp()}-non-full-unlock"
    target.mkdir(parents=True, exist_ok=True)
    for path in existing:
        shutil.copy2(path, target / path.name)
    return target


def _restore(baseline: Path, save_dir: Path) -> None:
    """Replace the live progress and its game-side backup with the frozen bytes."""
    save_dir.mkdir(parents=True, exist_ok=True)
    payload = baseline.read_bytes()
    for name in SAVE_FILES:
        (save_dir / name).write_bytes(payload)


def ensure_full_unlock(root: Path, *, restore: bool = True, save_dir: Path | None = None,
                       baseline: Path | None = None, running: bool | None = None,
                       client_id: str | None = None) -> dict:
    """Verify the launch profile and, when allowed, restore the frozen full-unlock baseline."""
    root = Path(root).resolve()
    if save_dir is None:
        save_dir = saves_dir(root, client_id)
        if save_dir is None:
            # Without machine.json this project cannot know which save a launch would use.
            return {"status": "skipped", "reason": "configs/machine.json is missing"}
    save_dir = Path(save_dir)
    progress = save_dir / "progress.save"
    try:
        counters = inspect_progress(progress)
    except FileNotFoundError:
        counters = None
    except (OSError, ValueError) as error:
        raise SaveGuardError(f"主档进度不可读：{error}") from None
    if counters is not None and is_full_unlock(counters):
        return {"status": "ok", "progress": str(progress), "counters": counters}
    if not restore:
        raise SaveGuardError(_mismatch(counters, progress))
    baseline = Path(baseline) if baseline is not None else root / BASELINE
    if not baseline.is_file():
        raise SaveGuardError(f"缺少全解锁基线存档：{baseline}")
    if running is None:
        running = game_running()
    if running is not False:
        # Unknown process state is treated as running: a live profile must never be overwritten.
        detail = "无法确认游戏已关闭" if running is None else "游戏正在运行"
        raise SaveGuardError(f"{detail}，不能替换存档；请先关闭游戏，再重新启动仪表盘或游戏。")
    backup = _backup(save_dir, root)
    _restore(baseline, save_dir)
    restored = inspect_progress(progress)
    if not is_full_unlock(restored):
        raise SaveGuardError(f"恢复后校验失败：{restored}")
    return {"status": "restored", "progress": str(progress), "before": counters,
            "counters": restored, "backup": str(backup) if backup else None,
            "baseline": str(baseline), "baseline_sha256": _sha256(baseline)}
