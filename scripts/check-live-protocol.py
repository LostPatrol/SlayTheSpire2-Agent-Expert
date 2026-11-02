"""Verify cached replay and stale-token rejection after a stopped live smoke segment."""

import argparse
import json
from pathlib import Path
import uuid

from sts2_agent.bridge import Bridge, BridgeError
from sts2_agent.config import load_config
from sts2_agent.telemetry import write_json


def main() -> None:
    """Replay only a confirmed action ID and send one deliberately stale non-admitted request."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    directory = args.run_directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if result["status"] != "budget_stop":
        raise RuntimeError("Use a stopped, successfully bounded smoke segment.")
    requests = [json.loads(line) for line in (directory / "execution-requests.jsonl").read_text(encoding="utf-8").splitlines()]
    command = requests[-1]
    bridge = Bridge(load_config("configs/smoke.json", root))
    try:
        health = bridge.health()
        if health["external_protocol"]["instance_id"] != manifest["bridge"]["external_protocol"]["instance_id"]:
            raise RuntimeError("Bridge process changed; old in-memory receipts are gone.")
        original = bridge.receipt(command["action_id"])
        if original["status"] != "completed":
            raise RuntimeError("The recorded action is not confirmed completed.")
        before, _ = bridge.settled()
        duplicate = bridge.execute(command["action"], command["action_id"], command["expected_observation_token"])
        after_duplicate, _ = bridge.settled()
        if before["action_revision"] != after_duplicate["action_revision"]:
            raise RuntimeError("Duplicate submission changed the action revision.")
        try:
            bridge.execute({"type": "end_turn"}, str(uuid.uuid4()), "deliberately-stale-token")
        except BridgeError as error:
            if error.code != "stale_observation":
                raise
        else:
            raise RuntimeError("A stale observation was admitted.")
        after_stale, _ = bridge.settled()
        if before["action_revision"] != after_stale["action_revision"]:
            raise RuntimeError("Stale submission changed the action revision.")
        evidence = {"same_process": True, "original_action_id": command["action_id"],
            "duplicate_status": duplicate["status"], "duplicate_did_not_execute": True,
            "stale_rejected": True, "action_revision_unchanged": True}
        write_json(directory / "protocol-verification.json", evidence)
        print(json.dumps(evidence, indent=2))
    finally:
        bridge.client.close()


if __name__ == "__main__":
    main()
