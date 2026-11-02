"""Keep optional native simulation state out of public model observations."""

from copy import deepcopy

import pytest

from sts2_agent.bridge import Bridge, BridgeError
from sts2_agent.public import observation


# The bridge uses a dummy loopback address because transport is replaced below.
CONFIG = {"bridge": {"base_url": "http://127.0.0.1:1"}, "simulation": {"read_access": "public"}}


def make_bridge(payload, mode="public"):
    """Capture outgoing query parameters without contacting a game process."""
    config = deepcopy(CONFIG)
    config["simulation"]["read_access"] = mode
    bridge = Bridge(config)
    calls = []

    def request(method, path, **kwargs):
        """Return a detached synthetic response and record the requested lane."""
        calls.append((method, path, kwargs))
        return deepcopy(payload)

    bridge._request = request
    return bridge, calls


def test_default_does_not_request_or_retain_internal_state():
    """Even an unsolicited private sibling never appears in the public result."""
    payload = {"public_state": {"protocol_version": 1}, "public_observation_token": "root",
               "simulation_internal": {"schema_version": 1, "read_access": "internal", "rng": {"run_seed": 99}}}
    bridge, calls = make_bridge(payload)
    assert bridge.snapshot() == ({"protocol_version": 1}, "root")
    assert calls == [("GET", "/decision-snapshot", {})]
    assert bridge.simulation_internal is None
    assert bridge.simulation_internal_token is None
    bridge.client.close()


def test_internal_lane_is_detached_bound_and_cleared_on_mode_switch():
    """Changing config takes effect immediately and removes stale privileged data."""
    private = {"schema_version": 1, "read_access": "internal", "rng": {"run_seed": 99}}
    payload = {"public_state": {"protocol_version": 1}, "public_observation_token": "root",
               "simulation_internal": private}
    bridge, calls = make_bridge(payload, "internal")
    public, token = bridge.snapshot()
    assert calls[0][2] == {"params": {"read_access": "internal"}}
    assert bridge.simulation_internal == private
    assert bridge.simulation_internal is not private
    assert bridge.simulation_internal_token == token
    assert "rng" not in public
    bridge.full_config["simulation"]["read_access"] = "public"
    bridge.snapshot()
    assert calls[-1][2] == {}
    assert bridge.simulation_internal is None
    assert bridge.simulation_internal_token is None
    bridge.client.close()


def test_public_mentor_calibration_remains_private_and_bound():
    """Public search receives calibration only through the private, exact-root channel."""
    private = {"schema_version": 1, "read_access": "internal", "mentor_state": {"rng": {"seed": 99}}}
    payload = {"public_state": {"protocol_version": 1}, "public_observation_token": "root",
               "simulation_internal": private}
    bridge, calls = make_bridge(payload)
    bridge.full_config["simulation"].update(enabled=True, backend="mentor")
    try:
        public, token = bridge.snapshot()
        assert calls[-1][2] == {"params": {"read_access": "internal"}}
        assert bridge.simulation_internal == private
        assert bridge.simulation_internal_token == token
        assert public == {"protocol_version": 1}
        payload.pop("simulation_internal")
        assert bridge.snapshot() == (public, token)
        assert bridge.simulation_internal is None
    finally:
        bridge.client.close()


def test_enemy_instance_identity_reaches_policy_when_slots_reorder():
    """Preserve the bridge's reviewed labels instead of identifying same-species enemies by slot."""
    state = {"protocol_version": 1, "available_actions": ["end_turn"], "combat": {"enemies": [
        {"i": 0, "enemy_id": "NIBBIT", "instance_id": "enemy-1"},
        {"i": 1, "enemy_id": "NIBBIT", "instance_id": "enemy-2"}]}}
    payload = {"public_state": state, "public_observation_token": "before"}
    bridge, _ = make_bridge(payload)
    first, _ = bridge.snapshot()
    payload["public_state"]["combat"]["enemies"] = [
        {"i": 0, "enemy_id": "NIBBIT", "instance_id": "enemy-2"}]
    payload["public_observation_token"] = "after"
    second, _ = bridge.snapshot()
    assert observation(first, "run", "episode")["state"]["combat"]["enemies"][1]["instance_id"] == "enemy-2"
    assert observation(second, "run", "episode")["state"]["combat"]["enemies"][0]["instance_id"] == "enemy-2"
    bridge.client.close()


@pytest.mark.parametrize("private", [None, {}, {"schema_version": 2},
                                      {"schema_version": 1, "read_access": "public"}])
def test_internal_mode_never_silently_accepts_an_old_or_public_bridge(private):
    """A failed opt-in request clears the previous privileged observation."""
    payload = {"public_state": {"protocol_version": 1}, "public_observation_token": "root",
               "simulation_internal": private}
    bridge, _ = make_bridge(payload, "internal")
    bridge.simulation_internal = {"old": True}
    bridge.simulation_internal_token = "old"
    with pytest.raises(BridgeError, match="simulation_internal"):
        bridge.snapshot()
    assert bridge.simulation_internal is None
    assert bridge.simulation_internal_token is None
    bridge.client.close()
