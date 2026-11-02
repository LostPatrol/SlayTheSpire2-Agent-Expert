"""Verify instruction lifetime, stable reservations, and durable assistance evidence."""

import pytest

from sts2_agent.instructions import InstructionBook


def test_one_shot_reservation_survives_retries_and_consumes_once():
    """Retries reuse the same instruction while another decision cannot claim it."""
    book = InstructionBook()
    instruction = book.add("Keep a potion", "next_decision")
    first = book.reserve("decision-one", "combat-one")
    assert first == book.reserve("decision-one", "combat-one")
    assert book.reserve("decision-two", "combat-one") == []
    book.accept("decision-two")
    assert book.records[0]["status"] == "injected"
    book.accept("decision-one")
    book.accept("decision-one")
    assert book.records[0]["status"] == "consumed"
    assert book.records[0]["injected_decisions"] == ["decision-one"]
    assert book.reserve("decision-two") == []
    assert first == [{"id": instruction["id"], "text": "Keep a potion", "scope": "next_decision"}]


def test_release_and_revoke_do_not_revive_terminal_records_or_mutate_captured_payload():
    """Only live one-shot reservations return to the queue after cancellation."""
    book = InstructionBook()
    revoked = book.add("Be careful", "next_decision")
    consumed = book.add("Take a card", "next_decision")
    captured = book.reserve("old")
    book.revoke(revoked["id"])
    book.accept("old")
    book.release("old")
    assert [record["status"] for record in book.records] == ["revoked", "consumed"]
    assert book.reserve("new") == []
    assert len(captured) == 2
    assert captured[0]["id"] == revoked["id"]
    assert captured[1]["id"] == consumed["id"]
    assert all(record["injected_decisions"] == ["old"] for record in book.records)


def test_canceled_one_shot_requeues_and_keeps_injection_history():
    """Canceled work does not consume guidance or erase earlier assistance."""
    book = InstructionBook()
    book.add("Pick defense", "next_decision")
    first = book.reserve("old")
    book.release("unrelated")
    assert book.records[0]["status"] == "injected"
    book.release("old")
    assert book.records[0]["status"] == "queued"
    assert book.reserve("new") == first
    book.accept("old")
    assert book.records[0]["status"] == "injected"
    book.accept("new")
    assert book.records[0]["status"] == "consumed"
    assert book.records[0]["injected_decisions"] == ["old", "new"]


def test_combat_scope_uses_stable_encounter_and_run_scope_persists():
    """Expiration cannot affect another encounter or a run-wide instruction."""
    book = InstructionBook()
    first = book.add("Focus left", "combat", "encounter-one")
    second = book.add("Focus right", "combat", "encounter-two")
    persistent = book.add("Prefer attacks", "run")
    assert [item["id"] for item in book.reserve("one", "encounter-one")] == [first["id"], persistent["id"]]
    book.accept("one")
    book.expire_encounter("encounter-one")
    assert [item["id"] for item in book.reserve("two", "encounter-two")] == [second["id"], persistent["id"]]
    assert [item["id"] for item in book.reserve("three", "encounter-one")] == [persistent["id"]]
    assert book.records[0]["status"] == "expired"


def test_restore_retains_assistance_history_and_all_public_records_are_detached():
    """Revoke and restart preserve evidence, while external mutation stays isolated."""
    book = InstructionBook()
    injected = book.add("Save gold", "run")
    queued = book.add("Save health", "run")
    book.revoke(queued["id"])
    book.reserve("first")
    book.revoke(injected["id"])
    snapshot = book.snapshot()
    restored = InstructionBook(snapshot)
    snapshot[0]["injected_decisions"].clear()
    visible = restored.records
    visible[0]["text"] = "changed"
    assert restored.records[0]["text"] == "Save gold"
    assert restored.records[0]["injected_decisions"] == ["first"]
    assert restored.records[1]["injected_decisions"] == []
    assert restored.reserve("later") == []


def test_active_payload_excludes_volatile_metadata_and_repeated_decisions_stay_stable():
    """Persistent guidance does not introduce per-decision provider cache churn."""
    book = InstructionBook()
    book.add("Consider block", "run")
    first = book.reserve("one")
    assert book.reserve("two") == first
    assert set(first[0]) == {"id", "text", "scope"}
    assert book.records[0]["injected_decisions"] == ["one", "two"]


@pytest.mark.parametrize("text,scope,encounter", [
    ("", "run", None), ("  ", "run", None), ("x" * 3001, "run", None),
    ("valid", "forever", None), ("valid", "combat", None), ("valid", "combat", "  "),
])
def test_invalid_instructions_are_rejected_with_safe_errors(text, scope, encounter):
    """Invalid operator input never becomes model guidance or leaks into errors."""
    book = InstructionBook()
    with pytest.raises(ValueError):
        book.add(text, scope, encounter)
    assert book.records == []
