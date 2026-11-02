"""Validate public draw uncertainty using exhaustive synthetic native orders and independent samples."""

from __future__ import annotations

import argparse
from collections import Counter
import itertools
import json
import math
from pathlib import Path
import sys
import time

from mentor_production_parity import DEFAULT_EXE, ROOT, file_hash
from native_parity import JsonlProcess, identifier
from parity_contract import canonical

# Each hidden order is created only in the synthetic oracle; the host receives its sorted public multiset.
DRAW_FAMILIES = (
    ("one_of_four", "SHRUG_IT_OFF", False, 1, ["STRIKE_IRONCLAD"] * 2 + ["DEFEND_IRONCLAD"] * 2),
    ("two_of_four", "POMMEL_STRIKE", True, 2, ["STRIKE_IRONCLAD"] * 2 + ["DEFEND_IRONCLAD"] * 2),
    ("three_of_six", "BATTLE_TRANCE", False, 3, ["STRIKE_IRONCLAD"] * 3 + ["DEFEND_IRONCLAD"] * 3),
    ("homogeneous", "SHRUG_IT_OFF", False, 1, ["STRIKE_IRONCLAD"] * 4),
)
# Match the documented independent sample indexing while avoiding repeated seed ranges across batches.
SCENARIO_STRIDE = 104729
# Report unusual marginals for investigation; this is not a proof of all stochastic behavior.
ALERT_P_VALUE = 0.01


def strike_count(endpoint):
    """Count the revealed post-action hand, never inspect hidden simulated pile order."""
    return sum(identifier(card.get("card_id", card.get("id"))) == "STRIKEIRONCLAD"
               for card in endpoint.get("hand", []))


def binomial_two_sided(successes, total, probability):
    """Compute an exact finite-binomial two-sided tail without adding statistical dependencies."""
    if probability in (0.0, 1.0):
        expected = total if probability else 0
        return 1.0 if successes == expected else 0.0
    masses = [math.comb(total, index) * probability ** index * (1 - probability) ** (total - index)
              for index in range(total + 1)]
    observed = masses[successes]
    return min(1.0, sum(mass for mass in masses if mass <= observed * (1 + 1e-12)))


def family_check(client, oracle, family, samples, seed):
    """Enumerate legal hidden orders, then compare independent sample frequencies to their exact mixture."""
    name, card, upgraded, draws, pile = family
    worlds, counts, roots = [], Counter(), set()
    root = None
    action = {"type": "play_card", "card_index": 0}
    if card == "POMMEL_STRIKE":
        action["target_index"] = 0
    for order in sorted(set(itertools.permutations(pile))):
        setup = {"hand": [{"card_id": card, "upgraded": upgraded}], "draw": list(order),
            "discard": [], "exhaust": [], "energy": 3, "hp": 80, "max_hp": 80,
            "encounter": "NIBBITS_NORMAL", "seed": "offline-exhaustive-draw-world"}
        initial = oracle.init(setup)
        after = oracle.step(action)
        source = initial["public_state"]
        roots.add(canonical(source))
        root = source
        count = strike_count(after["endpoint"])
        counts[count] += 1
        worlds.append({"setup": setup, "initial": initial, "action": action, "native_after": after,
                       "revealed_strikes": count})
    probabilities = {count: frequency / len(worlds) for count, frequency in counts.items()}
    strict = client.request({"op": "simulate", "public_state": root, "actions": [action]})
    mixed = len(probabilities) > 1
    strict_rejected = strict.get("ok") is not True
    strict_status = ("correct_rejection" if mixed and strict_rejected else "incorrect_exact_claim" if mixed
                     else "deterministic_subset_supported" if not strict_rejected else "avoidable_rejection")
    sampled, responses = Counter(), []
    completed = 0
    for offset in range(0, samples, 12):
        requested = min(12, samples - offset)
        response = client.request({"op": "evaluate", "public_state": root,
            "candidate_plans": [{"id": name, "actions": [action]}], "samples": requested,
            "max_actions": 8, "max_events": 512, "timeout_ms": 5000,
            "scenario_seed": seed + offset * SCENARIO_STRIDE})
        responses.append(response)
        if not response.get("ok"):
            continue
        for sample in response["candidates"][0]["samples"]:
            if sample["status"] == "completed":
                completed += 1
                sampled[strike_count(sample["state"])] += 1
    categories = sorted(set(probabilities) | set(sampled))
    diagnostics = [{"strike_count": count, "expected_probability": probabilities.get(count, 0.0),
        "observed_count": sampled[count], "sample_count": completed,
        "p_value": binomial_two_sided(sampled[count], completed, probabilities.get(count, 0.0))
                     if completed else None} for count in categories]
    # Bonferroni bounds the chance of one spurious marginal alarm within this family.
    threshold = ALERT_P_VALUE / max(1, len(categories))
    alarm = any(row["p_value"] is not None and row["p_value"] < threshold for row in diagnostics)
    return {"id": name, "draws": draws, "native_world_count": len(worlds),
        "public_roots_identical": len(roots) == 1, "distinct_public_root_count": len(roots),
        "native_exact_distribution": dict(counts), "native_worlds": worlds,
        "strict_status": strict_status, "strict_response": strict,
        "requested_samples": samples, "completed_samples": completed,
        "sampled_distribution": dict(sampled), "distribution_diagnostics": diagnostics,
        "familywise_alert_level": ALERT_P_VALUE, "per_marginal_threshold": threshold,
        "status": ("public_projection_order_leak" if len(roots) != 1 else "incomplete_samples" if completed != samples
                   else "distribution_issue" if alarm else "no_distribution_issue_detected"),
        "sample_responses": responses}


def main():
    """Run finite uncertainty checks without accessing a real game, save, model, or RNG stream."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--oracle-checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=240)
    parser.add_argument("--seed", type=int, default=99173)
    args = parser.parse_args()
    if not 12 <= args.samples <= 5000:
        parser.error("Use 12 to 5000 independent hypothetical samples per family")
    sys.path.insert(0, str(ROOT / "experiments/native_oracle"))
    from native_oracle import NativeOracle
    args.output.parent.mkdir(parents=True, exist_ok=True)
    paths = [args.exe, Path(__file__), ROOT / "experiments/native_oracle/native_oracle.py",
             args.oracle_checkout / "src/Sts2Headless/bin/Debug/net9.0/Sts2Headless.dll",
             args.oracle_checkout / "lib/sts2.dll"]
    artifacts = {str(path): file_hash(path) for path in paths}
    started, client = time.perf_counter(), JsonlProcess([str(args.exe.resolve())])
    try:
        with NativeOracle(args.oracle_checkout, runtime_dir=args.output.parent / "uncertainty-runtime") as oracle:
            cases = [family_check(client, oracle, family, args.samples, args.seed + index * 1_000_000_007)
                     for index, family in enumerate(DRAW_FAMILIES)]
    finally:
        client.close()
    result = {"scope": "uniform_mixture_of_controlled_synthetic_orders_not_real_run_conditional_odds",
        "hidden_information_policy": "Native setup order never enters production input; every world exports public multiset only.",
        "artifacts_before": artifacts, "artifacts_unchanged": all(file_hash(path) == value for path, value in artifacts.items()),
        "seed": args.seed, "elapsed_seconds": time.perf_counter() - started, "cases": cases,
        "summary": {"families": len(cases), "native_worlds": sum(case["native_world_count"] for case in cases),
            "strict": dict(Counter(case["strict_status"] for case in cases)),
            "distribution": dict(Counter(case["status"] for case in cases))}}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
