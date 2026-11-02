"""Load the NoSL controller configuration and optional public combat simulation."""

import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator


class ConfigError(ValueError):
    """A safe configuration error without credential or file contents."""


def load_config(path: str | Path, root: str | Path) -> dict:
    """Load a JSON configuration against the repository's published schema."""
    root = Path(root).resolve()
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        schema = json.loads((root / "configs" / "agent.schema.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("Cannot read configuration or configuration schema as JSON.") from None
    error = next(Draft202012Validator(schema).iter_errors(config), None)
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path) or "root"
        # Validator messages can contain arbitrary user input, including credentials.
        raise ConfigError(f"Invalid configuration at {location}; see configs/agent.schema.json.")
    strength = config.get("simulation", {}).get("search_strength")
    # Python's JSON parser accepts NaN, which ordinary schema bounds do not exclude.
    if type(strength) is float and not math.isfinite(strength):
        raise ConfigError("Invalid configuration at simulation.search_strength; see configs/agent.schema.json.")
    return config
