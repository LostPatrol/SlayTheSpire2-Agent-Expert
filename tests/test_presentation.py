"""Verify spectator language changes preserve actions, budgets, and restored run identity."""

from copy import deepcopy
import json
from pathlib import Path
import re

from fastapi.testclient import TestClient
import pytest

from sts2_agent.automatic import automatic_decision
from sts2_agent.config import ConfigError, load_config
from sts2_agent.dashboard import create_app
from sts2_agent.game_launcher import GameLauncher
from sts2_agent.provider import DECISION_PROMPT, decision_prompt
from sts2_agent.session import DashboardSession, SessionError, _action_label
from test_automatic import reward_observation
from test_game_launcher import harness, finish_launch, mark_terminal, assert_no_provider_calls
from test_provider import build, completion, DECISION, public_payload
from test_session_controls import FakeProvider


def test_optional_presentation_config_accepts_only_reviewed_languages(tmp_path):
    """The published config schema accepts either display language without changing defaults."""
    project_root = Path(__file__).resolve().parents[1]
    path = tmp_path / "presentation-config.json"
    for language in ("zh-CN", "en"):
        config = load_config("configs/smoke.json", project_root)
        config["presentation"] = {"language": language}
        path.write_text(json.dumps(config), encoding="utf-8")
        assert load_config(path, project_root)["presentation"]["language"] == language
    config["presentation"]["language"] = "fr"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, project_root)


def test_language_prompt_preserves_chinese_default_and_action_protocol(monkeypatch):
    """Only the system's spectator-language instruction differs between equivalent requests."""
    assert decision_prompt({}) == decision_prompt({"presentation": {"language": "zh-CN"}}) == DECISION_PROMPT
    requests = []
    providers = []
    for language in ("zh-CN", "en"):
        provider, captured = build(monkeypatch, [completion(json.dumps(DECISION))])
        provider.config["presentation"] = {"language": language}
        assert provider.decide({"available_actions": []}, {}) == DECISION
        providers.append(provider)
        requests.append(captured[0])
    chinese, english = requests
    assert public_payload(chinese["messages"]) == public_payload(english["messages"])
    assert {key: value for key, value in chinese.items() if key != "messages"} == {
        key: value for key, value in english.items() if key != "messages"}
    english_prompt = english["messages"][0]["content"]
    assert "concise Chinese" not in english_prompt
    assert "build_plan, scene_plan" in english_prompt and "reasoning text emitted by the API" in english_prompt
    assert providers[0]._prompt_metrics["prompt_policy_digest"] != providers[1]._prompt_metrics["prompt_policy_digest"]
    for provider in providers:
        provider.close()


def test_language_settings_are_explicit_and_preserve_budget_and_native_state(harness):
    """Reads never write a preference, and an explicit fresh-run change preserves policy spending."""
    preference = harness.root / "local/presentation-settings.json"
    before = harness.session.snapshot()
    native_before = deepcopy(harness.bridge.state)
    assert harness.launcher.snapshot()["presentation_settings"]["language"] == "zh-CN"
    assert not preference.exists()
    settings = harness.launcher.set_presentation_settings("en")
    assert settings == {"language": "en", "current_language": "en", "editable": True, "reason": None}
    after = harness.session.snapshot()
    assert after["presentation_language"] == "en"
    assert after["metrics"] == before["metrics"]
    assert after["run_id"] == before["run_id"]
    assert after["assistance_mode"] == before["assistance_mode"]
    assert harness.bridge.state == native_before and not harness.bridge.executions
    assert json.loads(preference.read_text(encoding="utf-8")) == {"language": "en"}
    assert_no_provider_calls(harness)


@pytest.mark.parametrize("busy", ["running", "model", "decision", "action", "pending_file", "started_menu", "active_screen"])
def test_language_changes_are_locked_for_started_or_busy_runs(harness, busy):
    """An unfinished run remains locked even when the native window is at the main menu."""
    if busy == "running":
        harness.session._state["desired_state"] = "running"
    elif busy in {"model", "decision"}:
        harness.session._state["request_pending" if busy == "model" else "decision_pending"] = True
    elif busy == "action":
        harness.session._state["inflight"] = {"action_id": "pending"}
    elif busy == "pending_file":
        (harness.root / "local/pending-action.json").write_text("{}", encoding="utf-8")
    elif busy == "started_menu":
        harness.session._state["metrics"]["actions_completed"] = 1
    else:
        harness.bridge.set_screen("EVENT")
        harness.session._observe(settled=True)
    assert harness.launcher.presentation_settings()["editable"] is False
    before = harness.session.snapshot()
    with pytest.raises(SessionError):
        harness.launcher.set_presentation_settings("en")
    assert harness.session.snapshot()["presentation_language"] == before["presentation_language"]
    assert not (harness.root / "local/presentation-settings.json").exists()


def test_language_settings_api_validates_and_remains_read_only_on_load(harness):
    """The settings endpoint rejects unsupported inputs and uses the session's edit gate."""
    app = create_app(harness.config, harness.root, session=harness.session)
    with TestClient(app, base_url="http://127.0.0.1:18766") as client:
        assert client.get("/api/snapshot").json()["presentation_settings"]["language"] == "zh-CN"
        assert not (harness.root / "local/presentation-settings.json").exists()
        for body in ({"language": "fr"}, {"language": True}, {}, {"language": "en", "extra": 1}):
            assert client.post("/api/presentation/settings", json=body).status_code == 422
        assert client.post("/api/presentation/settings", json={"language": "en"}).json()["presentation_settings"]["language"] == "en"
        harness.session._state["request_pending"] = True
        assert client.post("/api/presentation/settings", json={"language": "zh-CN"}).status_code == 409
        harness.session._state["request_pending"] = False


def test_existing_run_language_wins_over_next_run_preference(harness):
    """Restoration keeps the ledger language even when a later local preference differs."""
    harness.launcher.set_presentation_settings("en")
    (harness.root / "local/presentation-settings.json").write_text('{"language":"zh-CN"}', encoding="utf-8")
    restored = DashboardSession(harness.config, harness.root, bridge=harness.bridge, provider=FakeProvider())
    assert restored.snapshot()["presentation_language"] == "en"
    assert restored.config["presentation"]["language"] == "en"
    launcher = GameLauncher(harness.config, harness.root, restored)
    assert launcher.presentation_settings()["language"] == "en"
    restored.shutdown()


def test_legacy_run_without_language_restores_as_chinese(harness):
    """An older ledger does not inherit a new English preference on service restart."""
    ledger = harness.root / "local/dashboard-session.json"
    state = json.loads(ledger.read_text(encoding="utf-8"))
    state.pop("presentation_language")
    ledger.write_text(json.dumps(state), encoding="utf-8")
    (harness.root / "local/presentation-settings.json").write_text('{"language":"en"}', encoding="utf-8")
    restored = DashboardSession(harness.config, harness.root, bridge=harness.bridge, provider=FakeProvider())
    assert restored.snapshot()["presentation_language"] == "zh-CN"
    restored.shutdown()


def test_terminal_next_language_preserves_old_ledger_then_applies_after_archive(harness):
    """A next-run choice survives archive without rewriting the terminal run's language."""
    harness.launcher.set_presentation_settings("en")
    old_run, result, original_result = mark_terminal(harness)
    ledger = harness.root / "local/dashboard-session.json"
    original_ledger = ledger.read_bytes()
    settings = harness.launcher.set_presentation_settings("zh-CN")
    assert settings["language"] == "zh-CN" and settings["current_language"] == "en"
    assert ledger.read_bytes() == original_ledger
    assert harness.session.config["presentation"]["language"] == "en"
    harness.launcher.start("prepare-next-language-run")
    view = finish_launch(harness.launcher)
    assert view["game_launch"]["status"] == "ready"
    assert view["run_id"] != old_run
    assert view["presentation_language"] == view["presentation_settings"]["language"] == "zh-CN"
    assert result.read_bytes() == original_result
    archive = next((harness.root / "local/session-archives").glob("*.json"))
    assert json.loads(archive.read_text(encoding="utf-8"))["presentation_language"] == "en"
    assert_no_provider_calls(harness)


def test_terminal_language_preference_survives_service_restoration(harness):
    """Restarted launch settings retain the next-run preference while restoring the completed language."""
    harness.launcher.set_presentation_settings("en")
    mark_terminal(harness)
    harness.launcher.set_presentation_settings("zh-CN")
    restarted_config = deepcopy(harness.config)
    restarted_config.pop("presentation")
    restored = DashboardSession(restarted_config, harness.root, bridge=harness.bridge, provider=FakeProvider())
    launcher = GameLauncher(restarted_config, harness.root, restored)
    settings = launcher.presentation_settings()
    assert settings["language"] == "zh-CN" and settings["current_language"] == "en"
    assert settings["editable"] is True
    restored.shutdown()


def test_automatic_translation_and_frozen_action_names_preserve_handles():
    """Equivalent localized automatic proposals and labels retain their exact game action indices."""
    obs = reward_observation()
    chinese = automatic_decision(obs)
    english = automatic_decision(obs, {"presentation": {"language": "en"}})
    assert chinese["action"] == english["action"]
    assert english["decision_source"] == chinese["decision_source"] == "automatic"
    assert not re.search(r"[\u3400-\u9fff]", english["reason"])
    frozen = {"state": {"combat": {"hand": [{"i": 3, "name": "Strike"}],
                                  "enemies": [{"i": 1, "name": "Jaw Worm"}]}}}
    action = {"type": "play_card", "card_index": 3, "target_index": 1}
    assert _action_label(action, frozen, "en") == "Play Strike → Jaw Worm"
    assert _action_label(action, frozen) == "打出 Strike → Jaw Worm"
