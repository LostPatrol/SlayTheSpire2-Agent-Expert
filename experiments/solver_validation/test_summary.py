"""Guard the outcome denominator used in diagnostic policy comparisons."""

from benchmark import summarize


def test_cutoffs_and_errors_remain_in_win_fraction_denominator():
    """One terminal win among four attempted cells is a quarter, not a perfect record."""
    rows = [{"arm": "mcts-128", "response": response} for response in (
        {"status": "terminal", "final": {"player_won": True, "round": 3}},
        {"status": "terminal", "final": {"player_won": False, "round": 2}},
        {"status": "cutoff", "final": {"round": 12}},
        {"status": "error", "error": "host_crashed"},
    )]
    result = summarize(rows)["mcts-128"]
    assert result["attempted"] == 4
    assert result["win_fraction_all_attempts"] == 0.25
    assert result["terminal_fraction"] == 0.5
    assert result["statuses"] == {"terminal": 2, "cutoff": 1, "error": 1}
    assert result["max_observed_round"] == 12
