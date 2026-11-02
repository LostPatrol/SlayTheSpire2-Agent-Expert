"""Fixed arithmetic helpers; these do not simulate effects or generate actions."""

from math import comb


def draw_at_least_one(total: int, matching: int, draws: int,
                      known_top_matching: bool | None = None) -> dict:
    """Calculate an unordered sample without replacement, optionally conditioning its top."""
    assumptions = ["Uniform sampling without replacement from the supplied population.",
                   "This is arithmetic only; no game draw effects or shuffle rules are inferred."]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (total, matching, draws)):
        return {"supported": False, "probability": None, "assumptions": assumptions,
                "error": "Population, matching count, and draws must be integers."}
    if total < 0 or not 0 <= matching <= total or not 0 <= draws <= total:
        return {"supported": False, "probability": None, "assumptions": assumptions,
                "error": "Counts must satisfy 0 <= matching, draws <= total."}
    if known_top_matching is not None and not isinstance(known_top_matching, bool):
        return {"supported": False, "probability": None, "assumptions": assumptions,
                "error": "Known-top condition must be a boolean or None."}
    if known_top_matching is not None:
        if draws < 1 or total < 1 or (known_top_matching and matching < 1) or (
                not known_top_matching and matching == total):
            return {"supported": False, "probability": None, "assumptions": assumptions,
                    "error": "Known-top condition is incompatible with these counts."}
        assumptions.append("The caller legitimately knows whether the first drawn card matches.")
        if known_top_matching:
            return {"supported": True, "probability": 1.0, "assumptions": assumptions}
        total -= 1
        draws -= 1
    if draws == 0 or matching == 0:
        probability = 0.0
    elif draws > total - matching:
        probability = 1.0
    else:
        probability = 1 - comb(total - matching, draws) / comb(total, draws)
    return {"supported": True, "probability": probability, "assumptions": assumptions}


def verify_selected_plan(observation: dict, action: dict) -> dict:
    """Check one already-selected action's public legality and basic resource arithmetic."""
    result = {"status": "unsupported", "supported": False, "issues": [],
              "limitations": ["Card effects, triggers, damage, block, and future states are not simulated."]}
    if not isinstance(observation, dict) or not isinstance(action, dict):
        result["issues"].append("Observation and action must be objects.")
        return result
    available = observation.get("available_actions", [])
    if action.get("type") not in available:
        result["issues"].append("Selected action is not listed as currently available.")
        result["legal"] = False
        return result
    if action.get("type") != "play_card":
        result["limitations"].append("No arithmetic check is implemented for this action type.")
        return result
    state = observation.get("state", {})
    combat = state.get("combat", {}) if isinstance(state, dict) else {}
    hand = combat.get("hand", []) if isinstance(combat, dict) else []
    index = action.get("card_index")
    if not isinstance(index, int) or isinstance(index, bool) or not isinstance(hand, list):
        result["issues"].append("A valid public hand index is required.")
        result["legal"] = False
        return result
    card = next((item for item in hand if isinstance(item, dict) and item.get("i") == index), None)
    if card is None:
        result["issues"].append("Selected card is absent from the public hand.")
        result["legal"] = False
        return result
    player = combat.get("player", {})
    energy = player.get("energy") if isinstance(player, dict) else None
    cost = card.get("energy_cost")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in (energy, cost)):
        result["limitations"].append("Missing, variable, or unsupported energy cost; no cost is assumed.")
        return result
    result.update({"status": "partial", "legal": True, "energy_before": energy,
                   "energy_cost": cost, "energy_after_cost": energy - cost})
    if cost > energy:
        result["legal"] = False
        result["issues"].append("Printed energy cost exceeds current public energy.")
    if card.get("playable") is False:
        result["legal"] = False
        result["issues"].append("The public card entry is marked unplayable.")
    targets = card.get("targets")
    if isinstance(targets, list) and targets and action.get("target_index") not in targets:
        result["legal"] = False
        result["issues"].append("Selected target is absent from the public target list.")
    return result
