"""Commands for public inspection, provider probing, and bounded live smoke runs."""

import argparse
import json
from pathlib import Path

from .bridge import Bridge, BridgeError
from .config import ConfigError, load_config
from .controller import run
from .provider import Provider, ProviderError
from .public import observation
from .telemetry import utc_now, write_json


def main() -> int:
    """Expose only implemented local workflows and print safe results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--config", default="configs/smoke.json")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("inspect")
    subcommands.add_parser("probe")
    subcommands.add_parser("new-session")
    # Blocks or restores a non-full-unlock profile before any native launch.
    save_guard = subcommands.add_parser("ensure-save")
    save_guard.add_argument("--check", action="store_true")
    save_guard.add_argument("--client-id")
    reconciler = subcommands.add_parser("reconcile-completed")
    reconciler.add_argument("--action-id", required=True)
    runner = subcommands.add_parser("run")
    runner.add_argument("--max-actions", type=int)
    dashboard = subcommands.add_parser("dashboard")
    dashboard.add_argument("--port", type=int, default=18766)
    dashboard.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        config = load_config(args.config, root)
        if args.command == "reconcile-completed":
            from .session import reconcile_completed_action
            result = reconcile_completed_action(config, root, args.action_id)
            # The full public proof remains in the ledger; keep legacy Windows console output safe.
            summary = {key: value for key, value in result.items() if key != "receipt"}
            print(json.dumps(summary, ensure_ascii=True, indent=2))
            return 0
        if args.command == "new-session":
            from .session import archive_terminal_session
            result = archive_terminal_session(root)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "ensure-save":
            from .save_guard import ensure_full_unlock
            result = ensure_full_unlock(root, restore=not args.check, client_id=args.client_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "dashboard":
            from .dashboard import serve
            serve(config, root, port=args.port, open_browser=not args.no_browser)
            return 0
        if args.command == "probe":
            result = Provider(config, root).probe()
            write_json(root / "local" / "provider-probe.json", {"timestamp_utc": utc_now(), **result})
        elif args.command == "inspect":
            bridge = Bridge(config)
            try:
                health = bridge.health()
                state, _ = bridge.snapshot()
                result = {"health": health, "observation": observation(state, "inspection", "inspection")}
            finally:
                bridge.client.close()
        else:
            if args.max_actions is not None and args.max_actions < 1:
                parser.error("--max-actions must be positive")
            result = run(config, root, args.max_actions)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") != "technical_interruption" else 1
    except (ConfigError, ProviderError, BridgeError, RuntimeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
