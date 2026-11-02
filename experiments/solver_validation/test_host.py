"""Check continuous-trial determinism and honest cutoff reporting against a built host."""

import os
from pathlib import Path

from benchmark import DEFAULT_EXE, Host, corpus

# A validation build can be checked before any production deployment.
HOST_EXE = Path(os.environ.get("MENTOR_PRODUCTION_EXE", DEFAULT_EXE))


def request(**overrides):
    """Search the same public starter combat with a small deterministic simulation budget."""
    payload = {"op": "combat_trial", "public_state": corpus()[0]["public_state"],
        "policy": "mcts", "seed": 719, "simulations": 32, "max_rollout_depth": 200,
        "max_decisions": 150, "trial_timeout_ms": 30000}
    payload.update(overrides)
    host = Host(HOST_EXE, 40)
    try:
        return host.request(payload)
    finally:
        host.close()


def test_continuous_trial_is_reproducible_with_terminal_statistics_bounded():
    """Repeated fixed-seed public searches reproduce every transition, not only the first pick."""
    first, second = request(), request()
    assert first.get("status") == second.get("status") == "terminal", (first, second)
    for response in (first, second):
        assert response["rollouts"] >= response["depth_cap_hits"] >= 0
        assert response["decisions"] == len(response["trace"])
        assert response["final"]["round"] > 1
        assert 0 <= response["final"]["hp"] <= 80
        response.pop("elapsed_ms", None)
    assert first == second


def test_decision_cap_never_becomes_a_terminal_win():
    """One action cannot resolve this starter fight; its cutoff must remain unresolved."""
    response = request(max_decisions=1)
    assert response.get("status") == "cutoff", response
    assert response["stop_reason"] == "decision_cap"
    assert response["final"]["terminal"] is False
