"""Verify the full-unlock save guard against synthetic profiles and the frozen baseline."""

import json
from pathlib import Path

import pytest

from sts2_agent import save_guard


def write_progress(path: Path, *, epochs: int, locked: int = 0, cards: int = 578,
                   relics: int = 297, potions: int = 65, events: int = 62,
                   acts: int = 4, characters: int = 5) -> Path:
    """Write a minimal progress file with the requested unlock counters."""
    states = ["revealed"] * (epochs - locked) + ["not_obtained"] * locked
    payload = {
        "epochs": [{"id": f"EPOCH_{index}", "state": state} for index, state in enumerate(states)],
        "discovered_cards": [f"CARD.{index}" for index in range(cards)],
        "discovered_relics": [f"RELIC.{index}" for index in range(relics)],
        "discovered_potions": [f"POTION.{index}" for index in range(potions)],
        "discovered_events": [f"EVENT.{index}" for index in range(events)],
        "discovered_acts": [f"ACT.{index}" for index in range(acts)],
        "character_stats": [{"id": f"CHARACTER.{index}"} for index in range(characters)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def baseline(tmp_path):
    """Provide a frozen full-unlock baseline inside the temporary project root."""
    path = write_progress(tmp_path / save_guard.BASELINE, epochs=57)
    return path


def test_full_unlock_counts_accept_obtained_epochs(tmp_path):
    """Obtained epochs stay unlocked, so only not_obtained states block a launch."""
    progress = write_progress(tmp_path / "saves/progress.save", epochs=57)
    payload = json.loads(progress.read_text(encoding="utf-8"))
    payload["epochs"][0]["state"] = "obtained"
    progress.write_text(json.dumps(payload), encoding="utf-8")
    counters = save_guard.inspect_progress(progress)
    assert counters["epochs"] == 57 and counters["locked_epochs"] == 0
    assert save_guard.is_full_unlock(counters)


def test_partial_profile_is_backed_up_and_restored(tmp_path, baseline):
    """A partial profile is replaced by the baseline only after the old bytes are copied aside."""
    save_dir = tmp_path / "live/saves"
    partial = write_progress(save_dir / "progress.save", epochs=23, locked=16, cards=106,
                             relics=56, potions=26, events=20, acts=3, characters=2)
    partial_bytes = partial.read_bytes()
    (save_dir / "progress.save.backup").write_bytes(partial_bytes)
    result = save_guard.ensure_full_unlock(tmp_path, save_dir=save_dir, baseline=baseline,
                                           running=False)
    assert result["status"] == "restored"
    assert result["before"]["cards"] == 106
    assert save_guard.is_full_unlock(result["counters"])
    assert (save_dir / "progress.save").read_bytes() == baseline.read_bytes()
    assert (save_dir / "progress.save.backup").read_bytes() == baseline.read_bytes()
    backup_dir = Path(result["backup"])
    assert (backup_dir / "progress.save").read_bytes() == partial_bytes
    assert result["baseline_sha256"] == save_guard._sha256(baseline)


def test_full_profile_is_left_untouched(tmp_path, baseline):
    """An already full profile is reported without writing anything."""
    save_dir = tmp_path / "live/saves"
    progress = write_progress(save_dir / "progress.save", epochs=57)
    original = progress.read_bytes()
    result = save_guard.ensure_full_unlock(tmp_path, save_dir=save_dir, baseline=baseline,
                                           running=False)
    assert result["status"] == "ok"
    assert progress.read_bytes() == original
    assert not (tmp_path / save_guard.BACKUP_ROOT).exists()


def test_restore_refused_while_game_is_running(tmp_path, baseline, monkeypatch):
    """A live game must never have its in-memory profile overwritten underneath it."""
    save_dir = tmp_path / "live/saves"
    write_progress(save_dir / "progress.save", epochs=23, locked=16, cards=106)
    with pytest.raises(save_guard.SaveGuardError, match="游戏正在运行"):
        save_guard.ensure_full_unlock(tmp_path, save_dir=save_dir, baseline=baseline, running=True)
    # An unavailable process check must fail closed instead of guessing the game is closed.
    monkeypatch.setattr(save_guard, "game_running", lambda: None)
    with pytest.raises(save_guard.SaveGuardError, match="无法确认游戏已关闭"):
        save_guard.ensure_full_unlock(tmp_path, save_dir=save_dir, baseline=baseline)


def test_check_mode_reports_without_restoring(tmp_path, baseline):
    """Explicit check mode fails loudly instead of replacing a deviating profile."""
    save_dir = tmp_path / "live/saves"
    write_progress(save_dir / "progress.save", epochs=23, locked=16, cards=106)
    with pytest.raises(save_guard.SaveGuardError, match="不是全解锁存档"):
        save_guard.ensure_full_unlock(tmp_path, restore=False, save_dir=save_dir,
                                      baseline=baseline, running=False)


def test_missing_progress_file_is_restored(tmp_path, baseline):
    """A missing profile still ends in the authorized full-unlock bytes."""
    save_dir = tmp_path / "live/saves"
    result = save_guard.ensure_full_unlock(tmp_path, save_dir=save_dir, baseline=baseline,
                                           running=False)
    assert result["status"] == "restored" and result["backup"] is None
    assert save_guard.inspect_progress(save_dir / "progress.save")["cards"] == 578


def test_missing_baseline_is_a_hard_error(tmp_path):
    """A deviating profile cannot be repaired without the frozen bytes it is supposed to restore."""
    save_dir = tmp_path / "live/saves"
    write_progress(save_dir / "progress.save", epochs=23, locked=16, cards=106)
    with pytest.raises(save_guard.SaveGuardError, match="缺少全解锁基线存档"):
        save_guard.ensure_full_unlock(tmp_path, save_dir=save_dir,
                                      baseline=tmp_path / "missing.save", running=False)


def test_missing_machine_config_skips_guard(tmp_path):
    """Without machine.json this project cannot resolve a save, so the guard reports a skip."""
    assert save_guard.saves_dir(tmp_path) is None
    assert save_guard.ensure_full_unlock(tmp_path)["status"] == "skipped"


def test_active_profile_selects_save_directory(tmp_path, monkeypatch):
    """The guard follows the profile the game will load instead of assuming profile 1."""
    client = tmp_path / "SlayTheSpire2/default/2026102701"
    (client / "modded/profile2/saves").mkdir(parents=True)
    (client / "profile.save").write_text('{"last_profile_id": 2, "schema_version": 2}',
                                         encoding="utf-8")
    (tmp_path / save_guard.MACHINE).parent.mkdir(parents=True)
    (tmp_path / save_guard.MACHINE).write_text('{"client_id": "2026102701"}', encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert save_guard.saves_dir(tmp_path) == client / "modded/profile2/saves"


def test_explicit_client_override_selects_that_client(tmp_path, monkeypatch):
    """A launch-time client override must be checked instead of the configured default."""
    (tmp_path / save_guard.MACHINE).parent.mkdir(parents=True)
    (tmp_path / save_guard.MACHINE).write_text('{"client_id": "2026102701"}', encoding="utf-8")
    client = tmp_path / "SlayTheSpire2/default/2026102901/modded/profile1/saves"
    client.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert save_guard.saves_dir(tmp_path, "2026102901") == client
