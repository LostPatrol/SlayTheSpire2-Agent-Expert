"""Track human instruction scope, reservations, and durable injection history."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4


# Limit operator input without interpreting natural language as executor constraints.
MAX_INSTRUCTION_LENGTH = 3000
# Only these scopes can enter model instruction snapshots.
INSTRUCTION_SCOPES = frozenset({"next_decision", "combat", "run"})
# Reserved persistent instructions remain eligible for later complete decisions.
ACTIVE_STATUSES = frozenset({"queued", "injected"})


class InstructionBook:
    """Manage records under the owning session's lock; never call models or games."""

    def __init__(self, records: list[dict] | None = None):
        """Restore detached records, retaining evidence of past model injections."""
        self._records = deepcopy(records) if records is not None else []
        for record in self._records:
            history = record.setdefault("injected_decisions", [])
            previous_decision = record.get("decision_id")
            if previous_decision and record.get("status") in {"injected", "consumed"}:
                if previous_decision not in history:
                    history.append(previous_decision)

    @property
    def records(self) -> list[dict]:
        """Return detached records suitable for UI display or persistence."""
        return self.snapshot()

    def add(self, text: str, scope: str, encounter_id: str | None = None) -> dict:
        """Queue a bounded plain-text instruction for a supported lifetime."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Instruction text must not be empty.")
        if len(text) > MAX_INSTRUCTION_LENGTH:
            raise ValueError("Instruction text must be at most 3000 characters.")
        if not isinstance(scope, str) or scope not in INSTRUCTION_SCOPES:
            raise ValueError("Unsupported instruction scope.")
        if scope == "combat" and (not isinstance(encounter_id, str) or not encounter_id.strip()):
            raise ValueError("Combat instructions require a current encounter.")
        record = {
            "id": str(uuid4()),
            "text": text.strip(),
            "scope": scope,
            "status": "queued",
            "encounter_id": encounter_id if scope == "combat" else None,
            "decision_id": None,
            "injected_decisions": [],
            "received_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._records.append(record)
        return deepcopy(record)

    def revoke(self, instruction_id: str) -> dict:
        """Exclude future snapshots while preserving prior injection evidence."""
        for record in self._records:
            if record["id"] == instruction_id:
                record["status"] = "revoked"
                return deepcopy(record)
        raise ValueError("Instruction was not found.")

    def reserve(self, decision_id: str, encounter_id: str | None = None) -> list[dict]:
        """Reserve eligible instructions and return stable, minimal model payloads."""
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise ValueError("A decision identifier is required.")
        selected = []
        for record in self._records:
            if record["status"] not in ACTIVE_STATUSES:
                continue
            if record["scope"] == "combat" and record["encounter_id"] != encounter_id:
                continue
            if (record["scope"] == "next_decision" and record.get("decision_id")
                    and record["decision_id"] != decision_id):
                continue
            record["status"] = "injected"
            record["decision_id"] = decision_id
            if decision_id not in record["injected_decisions"]:
                record["injected_decisions"].append(decision_id)
            selected.append({key: record[key] for key in ("id", "text", "scope")})
        return selected

    def accept(self, decision_id: str) -> None:
        """Consume one-shot instructions after the first confirmed accepted action."""
        for record in self._records:
            if (record["scope"] == "next_decision" and record["status"] == "injected"
                    and record.get("decision_id") == decision_id):
                record["status"] = "consumed"

    def release(self, decision_id: str) -> None:
        """Return canceled one-shot reservations to the queue without reviving terminals."""
        for record in self._records:
            if (record["scope"] == "next_decision" and record["status"] == "injected"
                    and record.get("decision_id") == decision_id):
                record["status"] = "queued"
                record["decision_id"] = None

    def expire_encounter(self, encounter_id: str) -> None:
        """Expire active combat guidance belonging to this stable encounter only."""
        for record in self._records:
            if (record["scope"] == "combat" and record["encounter_id"] == encounter_id
                    and record["status"] in ACTIVE_STATUSES):
                record["status"] = "expired"

    def snapshot(self) -> list[dict]:
        """Copy the complete durable record history without exposing mutable state."""
        return deepcopy(self._records)
