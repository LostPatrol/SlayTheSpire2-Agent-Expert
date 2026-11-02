"""Regression tests for localized names in the simulation adapter."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "local/simulation/mentor-native/python"))
sys.path.insert(0, str(ROOT / "vendor/spire-mentor/sts2-pycli/tests/fixtures/hint_adapter"))

from sts2_hint import adapter  # noqa: E402
from realgame_adapter import _orb_type_stem  # noqa: E402
from _helpers import base_state, card, enemy, player_summary  # noqa: E402


def _state_with_potion(potion: dict) -> dict:
    """Build a minimal combat board carrying one potion."""
    state = base_state(
        player=player_summary(potions=[potion]),
        hand=[card(index=0, cid="IRONCLAD/STRIKE", name="Strike")],
        enemies=[enemy(index=0, name="Nibbit", hp=48, max_hp=48)],
    )
    return state


def test_localized_potion_prefers_stable_id():
    """Chinese display names must not hide a potion with a stable bridge id."""
    out = adapter.translate(_state_with_potion({"id": "FIRE_POTION", "name": "火焰药水"}))
    assert out["player"]["potions"] == ["FIRE_POTION"]


def test_english_potion_name_still_falls_back():
    """Older producers without an id keep the existing English title path."""
    out = adapter.translate(_state_with_potion({"name": "Fire Potion"}))
    assert out["player"]["potions"] == ["FIRE_POTION"]


def test_unknown_potion_still_soft_drops():
    """An unknown localized title is still surfaced as a soft drop."""
    out = adapter.translate(_state_with_potion({"name": "未知药水"}))
    assert out["player"]["potions"] == [None]


def test_localized_orb_prefers_stable_id():
    """Orb names are localized too; the id must win over the display title."""
    assert _orb_type_stem({"id": "LIGHTNING_ORB", "name": "闪电"}) == "Lightning"
    assert _orb_type_stem({"name": "Lightning"}) == "Lightning"
