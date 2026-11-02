"""Keep witnessed history available to rules without modifying native public-state identity."""

from copy import deepcopy
import json

from test_simulation import make_client, public_frame


def test_all_simulation_operations_keep_public_history_separate(tmp_path):
    """Search, prediction and evaluation receive the same independently recorded evidence."""
    observed = public_frame()
    original = deepcopy(observed["state"])
    observed["public_history"] = {"schema_version": 1, "encounter_id": "combat-one",
                                  "permafrost_first_power_used": True}
    client, log = make_client(tmp_path)
    action = {"type": "play_card", "card_index": 7, "target_index": 11}
    try:
        advice = client.advise(observed)
        prediction = client.predict(observed, action)
        evaluation = client.evaluate(observed, [{"id": "known-action", "actions": [action]}], samples=1)
    finally:
        client.close()
    requests = [json.loads(line)["request"] for line in log.read_text().splitlines()]
    assert {row["op"] for row in requests} == {"search", "simulate", "evaluate"}
    assert all(row["public_history"] == observed["public_history"] for row in requests)
    assert all("public_history" not in row["public_state"] for row in requests)
    assert observed["state"] == original
    assert all(report["public_history"] == observed["public_history"]
               for report in (advice, prediction, evaluation))


def test_missing_or_malformed_optional_history_does_not_disable_simulation(tmp_path):
    """Old observations remain usable and invalid derived metadata is not sent as JSON NaN."""
    observed = public_frame()
    observed["public_history"] = {"amount": float("nan")}
    client, log = make_client(tmp_path)
    try:
        report = client.predict(observed, {"type": "play_card", "card_index": 7, "target_index": 11})
    finally:
        client.close()
    assert report["status"] == "predicted"
    assert "public_history" not in json.loads(log.read_text().splitlines()[0])["request"]
