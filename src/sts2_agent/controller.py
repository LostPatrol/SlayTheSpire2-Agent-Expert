"""Run the same persistent controller from a terminal without a dashboard."""

from copy import deepcopy
import json
from pathlib import Path
import time
import uuid

from .session import DashboardSession


def run(config: dict, root: Path, max_actions: int | None = None) -> dict:
    """Continue one persisted budget and retain memory when the terminal run stops."""
    config = deepcopy(config)
    if max_actions is not None:
        config["limits"]["max_actions"] = min(max_actions, config["limits"]["max_actions"])
    session = DashboardSession(config, root)
    last_count = session.snapshot()["metrics"]["actions_completed"]
    try:
        session.start_background()
        session.command(str(uuid.uuid4()), "resume")
        while True:
            snapshot = session.snapshot()
            count = snapshot["metrics"]["actions_completed"]
            if count != last_count:
                print(f"{count:02d} native actions confirmed", flush=True)
                last_count = count
            if snapshot["status"] in {"budget_stop", "game_over", "error", "stopped"}:
                break
            if (Path(root) / "local" / "PAUSE").exists():
                session.command(str(uuid.uuid4()), "pause")
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        session.command(str(uuid.uuid4()), "pause")
    finally:
        session.shutdown()
    path = Path(root) / "runs" / session.snapshot()["run_id"] / "result.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    view = session.snapshot()
    return {"run_id": view["run_id"], "status": view["status"], "error": view.get("error")}
