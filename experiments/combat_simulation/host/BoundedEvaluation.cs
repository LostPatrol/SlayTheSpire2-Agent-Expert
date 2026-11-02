// Evaluates fixed public plans in independent hypothetical current-turn worlds.
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Sts2Emulator.Core;
using Sts2Emulator.GeneratedData;

namespace CombatSimulation;

public sealed partial class SimulationPosition
{
    // Only hand-local choices with reviewed continuation semantics are enabled.
    private static readonly HashSet<CardSelectionKind> ReviewedChoices =
        [CardSelectionKind.ExhaustFromHand, CardSelectionKind.ExhaustFromHandThenDraw,
         CardSelectionKind.UpgradeInHand];
    // These copies require combat history not recoverable from a plain grouped descriptor.
    private static readonly HashSet<string> HistoryDependentPileCards = ["Rampage", "FranticEscape"];
    // Bound malformed grouped counts without imposing normal deck-size restrictions.
    private const int MaximumPileCards = 500;
    // Revealed simulated cards receive new handles, never executable public indices.
    private int _revealedCards;

    /// <summary>Build a fresh hypothetical world from canonical public pile multisets.</summary>
    public string? PrepareScenario(JsonElement source, Random random)
    {
        var combat = Get(source, "combat");
        foreach (var (name, pile) in new[] { ("draw", State.DrawPile), ("discard", State.DiscardPile), ("exhaust", State.ExhaustPile) })
        {
            if (Get(combat, name).ValueKind != JsonValueKind.Array) return $"{name}:public_pile_not_observed";
            var parsed = new List<CardInstance>();
            foreach (var group in Items(Get(combat, name)))
            {
                var ids = Items(Get(group, "card_ids")).Select(x => x.GetString() ?? "").ToArray();
                if (ids.Length != 1 || !CardIds.TryGetValue(Normalize(ids[0]), out var def))
                    return $"{name}:unknown_or_ambiguous_card_identity";
                if (!ReviewedCards.Contains(def.Name)) return $"{name}:card_rule_not_reviewed:{ids[0]}";
                bool observedDamage = Items(Get(group, "dynamic_values")).Any(value =>
                    Text(value, "name") == "Damage" && Get(value, "base_value").ValueKind == JsonValueKind.Number);
                if (HistoryDependentPileCards.Contains(def.Name) && !(def.Name == "Rampage" && observedDamage))
                    return $"{name}:per_copy_history_not_imported:{ids[0]}";
                var descriptor = JsonNode.Parse(group.GetRawText())!.AsObject();
                descriptor["card_id"] = ids[0];
                descriptor["i"] = 0;
                var check = new SimulationPosition { State = State.Clone() };
                check.State.Hand.Clear();
                check.ReadHand(JsonSerializer.SerializeToElement(descriptor));
                if (check._hand[0].InitialGap is { } gap) return $"{name}:{gap}";
                var match = Regex.Match(Text(group, "line"), @"^[^\[]*?\*(\d+)(?=\s|\[|$)");
                int count = match.Success ? int.Parse(match.Groups[1].Value) : 1;
                if (count < 1 || parsed.Count + count > MaximumPileCards) return $"{name}:invalid_public_stack_count";
                for (int i = 0; i < count; i++) parsed.Add(check.State.Hand[0]);
            }
            // Canonicalize before assigning random positions; input order is never knowledge.
            var canonical = parsed.OrderBy(card => JsonSerializer.Serialize(card), StringComparer.Ordinal).ToList();
            pile.Clear();
            pile.AddRange(canonical);
        }
        for (int i = State.DrawPile.Count - 1; i > 0; i--)
        {
            int swap = random.Next(i + 1);
            (State.DrawPile[i], State.DrawPile[swap]) = (State.DrawPile[swap], State.DrawPile[i]);
        }
        return null;
    }

    /// <summary>Apply one declared action without searching for an alternative.</summary>
    public string ApplyScenario(SimAction action, Random random, out string? reason)
    {
        reason = null;
        if (Stopped || IsTerminal) { reason = "position_already_stopped_or_terminal"; return "completed"; }
        if (State.PendingSelection is { } selection)
        {
            if (action.Type != "select_card" || action.ChoiceIndex is null)
            { reason = "pending_choice_requires_declared_selection"; return "needs_decision"; }
            if (!ReviewedChoices.Contains(selection.Kind))
            { reason = "selection_rule_not_reviewed"; return "unsupported"; }
            int choice = action.ChoiceIndex.Value;
            if (choice < 0 || choice >= selection.Candidates.Count)
            { reason = "invalid_choice_index"; return "unsupported"; }
            int removed = selection.Kind == CardSelectionKind.UpgradeInHand ? -1 : selection.Candidates[choice];
            var clone = State.Clone();
            var enemies = clone.Enemies.ToArray();
            CombatEngine.Step(clone, choice, random);
            if (clone.AutoPlayQueue.Count > 0 || clone.Enemies.Any(enemy => !enemies.Contains(enemy)))
            { reason = "selection_automatic_effect_not_reviewed"; return "unsupported"; }
            clone.Enemies = [.. enemies];
            var identities = _hand.Where((_, i) => i != removed).ToList();
            InstallScenarioSuccessor(clone, identities);
            return "completed";
        }
        if (action.Type == "select_card") { reason = "no_pending_choice"; return "unsupported"; }
        if (action.Type == "end_turn") { Stopped = true; return "completed"; }
        if (action.Type == "use_potion") return ApplyPotion(action, out reason) ? "completed" : "unsupported";
        if (action.Type != "play_card") { reason = "unsupported_action_type"; return "unsupported"; }
        if (action.CardId is not null)
        {
            int match = _hand.FindIndex(identity => Normalize(identity.CardId) == Normalize(action.CardId));
            if (match < 0) { reason = "declared_card_not_in_revealed_hand"; return "needs_decision"; }
            action = action with { CardHandle = _hand[match].Handle, CardIndex = null };
        }
        int index = FindHand(action);
        reason = CheckTransition(action, out var successor, allowReveals: true, scenarioRandom: random);
        if (reason is not null || successor is null) return "unsupported";
        InstallScenarioSuccessor(successor, _hand.Where((_, i) => i != index).ToList());
        return "completed";
    }

    /// <summary>Keep surviving identities and label newly revealed cards as simulation-only.</summary>
    private void InstallScenarioSuccessor(CombatState successor, List<HandIdentity> survivors)
    {
        var hand = new List<HandIdentity>();
        foreach (var card in successor.Hand)
        {
            var def = Cards.Get(card.DefId);
            string id = def.Entry;
            int previous = survivors.FindIndex(identity => Normalize(identity.CardId) == Normalize(id));
            if (previous >= 0)
            {
                hand.Add(survivors[previous]);
                survivors.RemoveAt(previous);
            }
            else
            {
                string? gap = ReviewedCards.Contains(def.Name) ? null : "card_rule_not_reviewed";
                hand.Add(new HandIdentity($"s{_revealedCards++}", -1, id, "", gap, true, null, false));
            }
        }
        State = successor;
        _hand = hand;
        _transitionGaps.Clear();
    }

    /// <summary>Expose only the revealed hand choice, never sampled draw positions.</summary>
    private object? ChoiceSnapshot()
    {
        if (State.PendingSelection is not { } choice) return null;
        return new
        {
            kind = choice.Kind.ToString(), source_card_id = Cards.Get(choice.SourceCardDefId).Entry,
            candidates = choice.Candidates.Select((index, ordinal) => new
            {
                choice_index = ordinal, card_handle = _hand[index].Handle,
                card_id = _hand[index].CardId, upgraded = State.Hand[index].Upgraded,
            }).ToArray(),
            meaning = "revealed_simulated_choice_only_not_a_live_game_action",
        };
    }
}

/// <summary>Bounded fixed-plan execution with shared independent worlds and explicit failures.</summary>
public static class BoundedEvaluation
{
    // Keep transport callers within the same finite contract as the Python controller.
    private const int MaximumCandidates = 4;
    // A small number of worlds is an estimate, never a survival guarantee.
    private const int MaximumSamples = 12;
    // The horizon includes choices and potions as player actions.
    private const int MaximumActions = 8;
    // Count applied actions, card draws and random requests in each path.
    private const int MaximumEvents = 512;
    // The controller also enforces a subprocess deadline outside automatic settlement.
    private const int MaximumTimeoutMs = 10_000;
    // A fixed stride gives candidates identical reproducible, game-independent worlds.
    private const int ScenarioSeedStride = 104729;

    /// <summary>Parse both public action indices and hypothetical revealed-card selectors.</summary>
    public static SimAction ParseAction(JsonElement action) => new(
        action.GetProperty("type").GetString() ?? "", String(action, "card_handle"),
        Integer(action, "card_index"), Integer(action, "target_index"),
        Integer(action, "potion_index") ?? Integer(action, "option_index"),
        Integer(action, "choice_index"), String(action, "card_id"));

    /// <summary>Run the supplied plans without adding choices or optimizing sampled worlds.</summary>
    public static object Run(JsonElement request)
    {
        int samples = Limit(request, "samples", MaximumSamples, MaximumSamples);
        int maxActions = Limit(request, "max_actions", MaximumActions, MaximumActions);
        int maxEvents = Limit(request, "max_events", MaximumEvents, MaximumEvents);
        int timeout = Limit(request, "timeout_ms", 5000, MaximumTimeoutMs);
        int seed = Integer(request, "scenario_seed") ?? 0;
        var plans = request.GetProperty("candidate_plans").EnumerateArray().ToArray();
        if (plans.Length is < 1 or > MaximumCandidates) throw new ArgumentException("candidate_count_out_of_range");
        var publicState = request.GetProperty("public_state");
        var results = new List<object>();
        var total = Stopwatch.StartNew();
        int paths = 0, consumedEvents = 0;
        foreach (var plan in plans)
        {
            var actions = plan.GetProperty("actions").EnumerateArray().Select(ParseAction).ToArray();
            var reports = new List<object>();
            var complete = new List<SimulationPosition>();
            var statuses = new List<string>();
            for (int sample = 0; sample < samples; sample++)
            {
                var timer = Stopwatch.StartNew();
                var random = new ScenarioRandom(unchecked(seed + sample * ScenarioSeedStride), maxEvents, timeout);
                var position = SimulationPosition.Import(publicState, 0);
                var events = new List<object>();
                string status = "completed";
                string? reason = null;
                try
                {
                    reason = position.PrepareScenario(publicState, random);
                    if (reason is not null) status = "unsupported";
                    for (int index = 0; index < actions.Length && status == "completed"; index++)
                    {
                        if (position.IsTerminal || position.Stopped) break;
                        if (index >= maxActions) { status = "incomplete"; reason = "player_action_budget_exhausted"; break; }
                        random.Check();
                        int draws = position.State.CardsDrawnThisCombat;
                        // Every attempted action runs on a private position; failed budget probes cannot leak.
                        var branch = position.Clone();
                        status = branch.ApplyScenario(actions[index], random, out reason);
                        random.Consume(1 + Math.Max(0, branch.State.CardsDrawnThisCombat - draws));
                        if (status != "completed") break;
                        position = branch;
                        events.Add(new { action_index = index, action = actions[index], applied = true });
                    }
                    if (status == "completed" && position.State.PendingSelection is not null)
                    { status = "needs_decision"; reason = "pending_choice_requires_declared_selection"; }
                }
                catch (ScenarioLimitException limit) { status = limit.Status; reason = limit.Message; }
                catch (Exception error) { status = "unsupported"; reason = $"{error.GetType().Name}:{error.Message}"; }
                statuses.Add(status);
                if (status == "completed") complete.Add(position);
                paths++;
                consumedEvents += random.Consumed;
                reports.Add(new
                {
                    status, state = position.Snapshot(), events, stop_reason = reason,
                    elapsed_ms = timer.Elapsed.TotalMilliseconds, events_consumed = random.Consumed,
                    scenario_index = sample,
                });
            }
            object? metrics = null;
            if (complete.Count == samples)
            {
                var initial = SimulationPosition.Import(publicState, 0);
                metrics = new
                {
                    hp_loss = Range(complete.Select(p => initial.State.PlayerHp - p.State.PlayerHp)),
                    block = Range(complete.Select(p => p.State.PlayerBlock)),
                    energy = Range(complete.Select(p => p.State.Energy)),
                    stars = Range(complete.Select(p => p.State.Stars)),
                    enemy_hp = Range(complete.Select(p => p.State.Enemies.Sum(enemy => Math.Max(0, enemy.Hp)))),
                    hand_size = Range(complete.Select(p => p.State.Hand.Count)),
                    hand_card_counts = complete.SelectMany(p => p.State.Hand)
                        .Select(card => (card.DefId, card.Upgraded)).Distinct()
                        .OrderBy(card => card.DefId).ThenBy(card => card.Upgraded)
                        .Select(card => new
                        {
                            card_id = Cards.Get(card.DefId).Entry, upgraded = card.Upgraded,
                            count = Range(complete.Select(p => p.State.Hand.Count(instance =>
                                instance.DefId == card.DefId && instance.Upgraded == card.Upgraded))),
                        }).ToArray(),
                    meaning = "sampled_current_turn_resources_before_turn_end_not_survival_or_win_probability",
                };
            }
            results.Add(new
            {
                id = plan.GetProperty("id").GetString(),
                status = complete.Count == samples ? "estimated" : statuses.First(status => status != "completed"),
                sample_count = samples, completed_samples = complete.Count,
                samples = reports, metrics,
            });
        }
        return new
        {
            ok = true, candidates = results,
            scope = "fixed_plans_independent_public_multiset_scenarios_current_player_turn",
            rule_snapshot = "evangambit-v0.107.1-bounded-v1",
            assumptions = new[] { "unknown_draw_order_uniformly_sampled_from_canonical_public_multiset",
                "no_game_seed_rng_hidden_order_or_future_enemy_ai_imported",
                "no_automatic_choice_or_per_world_action_search",
                "missing_or_failed_samples_never_zero_filled_or_removed_from_mean_denominator" },
            budget = new { paths_consumed = paths, events_consumed = consumedEvents, max_actions = maxActions,
                max_events = maxEvents, timeout_ms = timeout,
                event_counting = "player_actions_card_draws_and_random_requests_not_every_engine_hook" },
            elapsed_ms = total.Elapsed.TotalMilliseconds,
        };
    }

    /// <summary>Describe all observations only when every requested sample completed.</summary>
    private static object Range(IEnumerable<int> values)
    {
        var array = values.ToArray();
        return new { min = array.Min(), max = array.Max(), mean = array.Average() };
    }

    /// <summary>Read a bounded positive integer or fail explicitly.</summary>
    private static int Limit(JsonElement source, string key, int fallback, int maximum)
    {
        int value = Integer(source, key) ?? fallback;
        return value >= 1 && value <= maximum ? value : throw new ArgumentException($"{key}_out_of_range");
    }

    /// <summary>Read only a declared numeric action parameter.</summary>
    private static int? Integer(JsonElement source, string key) => source.TryGetProperty(key, out var value)
        && value.ValueKind != JsonValueKind.Null ? value.GetInt32() : null;

    /// <summary>Read only a declared textual action parameter.</summary>
    private static string? String(JsonElement source, string key) => source.TryGetProperty(key, out var value)
        && value.ValueKind != JsonValueKind.Null ? value.GetString() : null;
}

/// <summary>Stops a hypothetical path without publishing its partially applied action.</summary>
internal sealed class ScenarioLimitException(string status, string message) : Exception(message)
{
    public string Status { get; } = status;
}

/// <summary>Independent simulation randomness with finite request and wall-clock budgets.</summary>
internal sealed class ScenarioRandom(int seed, int maximum, int timeoutMs) : Random(seed)
{
    // The timer belongs to this path, rather than the actual game or its RNG streams.
    private readonly Stopwatch _timer = Stopwatch.StartNew();
    public int Consumed { get; private set; }

    /// <summary>Check time even for a deterministic step with no random requests.</summary>
    public void Check()
    {
        if (_timer.ElapsedMilliseconds >= timeoutMs) throw new ScenarioLimitException("timeout", "path_timeout");
    }

    /// <summary>Charge sampled random requests, player actions and observed card draws.</summary>
    public void Consume(int count)
    {
        Check();
        if (count > maximum - Consumed) throw new ScenarioLimitException("incomplete", "resolution_event_budget_exhausted");
        Consumed += count;
    }

    /// <summary>Charge every random API currently used by reviewed core rules.</summary>
    public override int Next(int maxValue) { Consume(1); return base.Next(maxValue); }
    /// <summary>Charge every random API currently used by reviewed core rules.</summary>
    public override int Next(int minValue, int maxValue) { Consume(1); return base.Next(minValue, maxValue); }
    /// <summary>Charge every random API currently used by reviewed core rules.</summary>
    public override int Next() { Consume(1); return base.Next(); }
    /// <summary>Charge continuous sampling without touching game-owned streams.</summary>
    public override double NextDouble() { Consume(1); return base.NextDouble(); }
}
