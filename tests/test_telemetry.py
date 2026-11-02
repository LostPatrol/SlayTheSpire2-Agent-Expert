"""Verify atomic ledger replacement during transient and persistent Windows denial."""

import json
from pathlib import Path

import pytest

from sts2_agent import telemetry


@pytest.mark.parametrize("recover", [True, False])
def test_replace_denial_preserves_complete_ledger(tmp_path, monkeypatch, recover):
    """Retry transient denial and retain both complete versions if replacement remains blocked."""
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{"actions": 349}', encoding="utf-8")
    original = Path.replace
    attempts = []

    def replace(source, target):
        """Deny replacement twice or permanently without touching the original file."""
        attempts.append(source)
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"actions": 349}
        if not recover or len(attempts) <= 2:
            raise PermissionError("Windows reader denied replacement")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(telemetry.time, "sleep", lambda _: None)
    if recover:
        telemetry.write_json(ledger, {"actions": 350})
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"actions": 350}
        assert len(attempts) == 3
    else:
        with pytest.raises(PermissionError):
            telemetry.write_json(ledger, {"actions": 350})
        assert len(attempts) == len(telemetry.REPLACE_RETRY_DELAYS) + 1
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"actions": 349}
        assert json.loads(ledger.with_suffix(".json.tmp").read_text(encoding="utf-8")) == {"actions": 350}
