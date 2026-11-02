// Current-turn strategy comparison over the adapter's deterministic public action graph.
// The simulation/search separation follows ing-gom/sts2-combat-ai (44f7d681); no upstream
// planner, hidden draw-order reads, card-ID deduplication, or heuristic legality filters are copied.
using System.Diagnostics;
using System.Text.Json;

namespace CombatSimulation;

/// <summary>Compares greedy, beam, and bounded exhaustive search with one score and action API.</summary>
public static class Search
{
    // Defaults keep the experiment's planning horizon and exhaustive comparison explicit.
    private const int DefaultMaxDepth = 6;
    private const int DefaultBeamWidth = 16;
    private const int DefaultExhaustiveLimit = 20000;
    // A finite recursion guard also bounds zero-cost or generated-card sequences.
    private const int MaximumDepth = 64;
    // Floating-point ties prefer the shorter plan, then the adapter's stable enumeration order.
    private const double ScoreTolerance = 1e-9;

    /// <summary>Runs one strategy without modifying the caller's simulation position.</summary>
    public static object Run(SimulationPosition initial, JsonElement request)
    {
        var elapsed = Stopwatch.StartNew();
        SearchSession? session = null;
        try
        {
            var options = ReadOptions(request);
            session = new SearchSession(initial.Clone(), options);
            session.ObserveSupport(session.Initial);
            if (options.Strategy == "exhaustive")
            {
                var countElapsed = Stopwatch.StartNew();
                session.CountPlans(session.Initial, 0);
                session.CountElapsedMs = countElapsed.Elapsed.TotalMilliseconds;
                if (session.CountTerminals > options.ExhaustiveLimit)
                {
                    return session.Result(elapsed.Elapsed.TotalMilliseconds, skipped: true,
                        reason: "exhaustive_terminal_limit_exceeded");
                }
            }

            var searchElapsed = Stopwatch.StartNew();
            session.Nodes = 1;
            switch (options.Strategy)
            {
                case "greedy":
                    session.Greedy();
                    break;
                case "beam":
                    session.Beam();
                    break;
                case "exhaustive":
                    session.Exhaustive(session.Initial, [], 0);
                    break;
            }
            session.SearchElapsedMs = searchElapsed.Elapsed.TotalMilliseconds;
            return session.Result(elapsed.Elapsed.TotalMilliseconds, skipped: false, reason: null);
        }
        catch (Exception error) when (error is ArgumentException or InvalidOperationException)
        {
            if (session is not null)
            {
                return session.Result(elapsed.Elapsed.TotalMilliseconds, skipped: true,
                    reason: error.Message, ok: false);
            }
            return new { ok = false, skipped = true, reason = error.Message, best = (object?)null };
        }
    }

    /// <summary>Reads explicit finite search limits and transparent terminal-score weights.</summary>
    private static SearchOptions ReadOptions(JsonElement request)
    {
        string strategy = request.TryGetProperty("strategy", out var value)
            ? value.GetString() ?? "beam" : "beam";
        if (strategy is not ("greedy" or "beam" or "exhaustive"))
            throw new ArgumentException("strategy must be greedy, beam, or exhaustive");
        int depth = ReadInteger(request, "max_depth", DefaultMaxDepth);
        int width = ReadInteger(request, "beam_width", DefaultBeamWidth);
        int limit = ReadInteger(request, "exhaustive_limit", DefaultExhaustiveLimit);
        if (depth < 0 || depth > MaximumDepth)
            throw new ArgumentException($"max_depth must be between 0 and {MaximumDepth}");
        if (width < 1)
            throw new ArgumentException("beam_width must be positive");
        if (limit < 1)
            throw new ArgumentException("exhaustive_limit must be positive");
        var weights = new ScoreWeights();
        if (request.TryGetProperty("score_weights", out var weightValues))
        {
            if (weightValues.ValueKind != JsonValueKind.Object)
                throw new ArgumentException("score_weights must be an object");
            weights = new ScoreWeights(
                ReadWeight(weightValues, "enemy_hp_reduction", weights.EnemyHpReduction),
                ReadWeight(weightValues, "enemy_kill", weights.EnemyKill),
                ReadWeight(weightValues, "player_hp_change", weights.PlayerHpChange),
                ReadWeight(weightValues, "projected_hp_loss", weights.ProjectedHpLoss),
                ReadWeight(weightValues, "remaining_energy", weights.RemainingEnergy),
                ReadWeight(weightValues, "excess_block", weights.ExcessBlock),
                ReadWeight(weightValues, "win_bonus", weights.WinBonus),
                ReadWeight(weightValues, "death_penalty", weights.DeathPenalty));
        }
        return new SearchOptions(strategy, depth, width, limit, weights);
    }

    /// <summary>Reads a whole-number option without silently replacing malformed input.</summary>
    private static int ReadInteger(JsonElement source, string name, int fallback)
    {
        if (!source.TryGetProperty(name, out var value)) return fallback;
        if (!value.TryGetInt32(out int result))
            throw new ArgumentException($"{name} must be an integer");
        return result;
    }

    /// <summary>Reads a finite configurable weight; negative experimental weights are permitted.</summary>
    private static double ReadWeight(JsonElement source, string name, double fallback)
    {
        if (!source.TryGetProperty(name, out var value)) return fallback;
        if (!value.TryGetDouble(out double result) || !double.IsFinite(result))
            throw new ArgumentException($"score_weights.{name} must be a finite number");
        return result;
    }

    /// <summary>Recognizes the adapter's explicit current-turn stop action.</summary>
    private static bool IsStop(SimAction action) => action.Type == "end_turn";

    private sealed record SearchOptions(string Strategy, int MaxDepth, int BeamWidth,
        int ExhaustiveLimit, ScoreWeights Weights);

    // All strategies use these weights; the score is a heuristic, never an estimated win rate.
    private sealed record ScoreWeights(double EnemyHpReduction = 1, double EnemyKill = 12,
        double PlayerHpChange = 4, double ProjectedHpLoss = 4, double RemainingEnergy = 0.05,
        double ExcessBlock = 0.02, double WinBonus = 1000, double DeathPenalty = 10000);

    private sealed record Endpoint(SimulationPosition Position, List<SimAction> Actions,
        double Score, object Breakdown, string Termination);

    private sealed record FrontierNode(SimulationPosition Position, List<SimAction> Actions,
        double Score, long Order);

    /// <summary>Owns one request's counters, support observations, and best terminal candidate.</summary>
    private sealed class SearchSession
    {
        public SimulationPosition Initial { get; }
        public long Nodes { get; set; }
        public long CountTerminals { get; private set; }
        public double CountElapsedMs { get; set; }
        public double SearchElapsedMs { get; set; }

        // Only this request's public baseline is used to measure progress.
        private readonly SearchOptions options;
        private readonly int initialEnemyHp;
        private readonly int initialAliveEnemies;
        private readonly HashSet<string> supportReasons = [];
        private bool fullCoverage = true;
        private long terminals;
        private long horizonTerminals;
        private long countNodes;
        private long frontierOrder;
        private Endpoint? best;

        /// <summary>Captures the public starting point and leaves the caller's state untouched.</summary>
        public SearchSession(SimulationPosition initial, SearchOptions options)
        {
            Initial = initial;
            this.options = options;
            countNodes = options.Strategy == "exhaustive" ? 1 : 0;
            initialEnemyHp = initial.State.Enemies.Sum(enemy => Math.Max(0, enemy.Hp));
            initialAliveEnemies = initial.State.Enemies.Count(enemy => enemy.Hp > 0);
        }

        /// <summary>Retains partial-coverage explanations discovered in any explored branch.</summary>
        public void ObserveSupport(SimulationPosition position)
        {
            fullCoverage &= position.Support.Level == "exact";
            foreach (string reason in position.Support.Reasons) supportReasons.Add(reason);
        }

        /// <summary>Uses every adapter action and ensures an explicit stop without ID deduplication.</summary>
        private IReadOnlyList<SimAction> Actions(SimulationPosition position)
        {
            var actions = position.LegalActions().ToList();
            ObserveSupport(position);
            if (!actions.Any(IsStop)) actions.Add(new SimAction("end_turn"));
            return actions;
        }

        /// <summary>Applies an action to a clone and fails visibly if the adapter's contract breaks.</summary>
        private SimulationPosition Transition(SimulationPosition position, SimAction action,
            bool counting = false)
        {
            var child = position.Clone();
            if (counting) countNodes++; else Nodes++;
            if (!child.Apply(action, out string? error))
                throw new InvalidOperationException($"search_transition_failed:{action.Type}:{error}");
            ObserveSupport(child);
            return child;
        }

        /// <summary>Counts terminal strategies first and saturates at the configured limit plus one.</summary>
        public void CountPlans(SimulationPosition position, int depth)
        {
            if (CountTerminals > options.ExhaustiveLimit) return;
            if (position.IsTerminal || position.Stopped || depth >= options.MaxDepth)
            {
                CountTerminals++;
                return;
            }
            foreach (var action in Actions(position))
            {
                var child = Transition(position, action, counting: true);
                if (IsStop(action)) CountTerminals++;
                else CountPlans(child, depth + 1);
                if (CountTerminals > options.ExhaustiveLimit) return;
            }
        }

        /// <summary>Follows the best immediate terminal improvement, considering stop at every step.</summary>
        public void Greedy()
        {
            var current = Initial;
            var path = new List<SimAction>();
            var currentEnd = EvaluateEndpoint(current, path, 0);
            for (int depth = 0; depth < options.MaxDepth && !current.IsTerminal; depth++)
            {
                SimulationPosition? next = null;
                List<SimAction>? nextPath = null;
                Endpoint nextEnd = currentEnd;
                foreach (var action in Actions(current))
                {
                    if (IsStop(action)) continue; // Already evaluated as currentEnd.
                    var child = Transition(current, action);
                    var childPath = new List<SimAction>(path) { action };
                    var candidate = EvaluateEndpoint(child, childPath, depth + 1);
                    if (candidate.Score > nextEnd.Score + ScoreTolerance)
                    {
                        next = child;
                        nextPath = childPath;
                        nextEnd = candidate;
                    }
                }
                if (next is null) return;
                current = next;
                path = nextPath!;
                currentEnd = nextEnd;
            }
        }

        /// <summary>Ranks all generated successors before an explicit per-depth beam truncation.</summary>
        public void Beam()
        {
            var start = EvaluateEndpoint(Initial, [], 0);
            var frontier = new List<FrontierNode> { new(Initial, [], start.Score, frontierOrder++) };
            for (int depth = 0; depth < options.MaxDepth && frontier.Count > 0; depth++)
            {
                var successors = new List<FrontierNode>();
                foreach (var node in frontier)
                {
                    if (node.Position.IsTerminal) continue;
                    foreach (var action in Actions(node.Position))
                    {
                        if (IsStop(action)) continue; // The node's stop was scored when generated.
                        var child = Transition(node.Position, action);
                        var path = new List<SimAction>(node.Actions) { action };
                        var endpoint = EvaluateEndpoint(child, path, depth + 1);
                        if (!child.IsTerminal && depth + 1 < options.MaxDepth)
                            successors.Add(new FrontierNode(child, path, endpoint.Score, frontierOrder++));
                    }
                }
                frontier = successors.OrderByDescending(node => node.Score)
                    .ThenBy(node => node.Actions.Count).ThenBy(node => node.Order)
                    .Take(options.BeamWidth).ToList();
            }
        }

        /// <summary>Visits every supported terminal plan after the preflight count has succeeded.</summary>
        public void Exhaustive(SimulationPosition position, List<SimAction> path, int depth)
        {
            if (position.IsTerminal || position.Stopped || depth >= options.MaxDepth)
            {
                EvaluateEndpoint(position, path, depth);
                return;
            }
            foreach (var action in Actions(position))
            {
                var child = Transition(position, action);
                var childPath = new List<SimAction>(path) { action };
                if (IsStop(action)) EvaluateStopped(child, childPath, "stop");
                else Exhaustive(child, childPath, depth + 1);
            }
        }

        /// <summary>Scores a real stop or combat terminal without advancing enemy AI or drawing.</summary>
        private Endpoint EvaluateEndpoint(SimulationPosition position, List<SimAction> path, int depth)
        {
            if (position.IsTerminal || position.Stopped)
                return EvaluateStopped(position, path, position.Stopped ? "stop" : "combat_terminal");
            string termination = depth >= options.MaxDepth ? "max_depth" : "stop";
            if (termination == "max_depth") horizonTerminals++;
            var stopped = Transition(position, new SimAction("end_turn"));
            return EvaluateStopped(stopped, new List<SimAction>(path) { new("end_turn") }, termination);
        }

        /// <summary>Computes the common transparent score and retains the best complete candidate.</summary>
        private Endpoint EvaluateStopped(SimulationPosition position, List<SimAction> path,
            string termination)
        {
            terminals++;
            var state = position.State;
            var weights = options.Weights;
            int alive = state.Enemies.Count(enemy => enemy.Hp > 0);
            int enemyHpReduction = initialEnemyHp - state.Enemies.Sum(enemy => Math.Max(0, enemy.Hp));
            int enemyKills = initialAliveEnemies - alive;
            int playerHpChange = state.PlayerHp - Initial.State.PlayerHp;
            // Minions can remain alive after every primary enemy is defeated.
            bool won = position.IsCombatWon;
            int incoming = won ? 0 : Math.Max(0, position.ProjectedIncomingDamage);
            int hpLeak = Math.Max(0, incoming - Math.Max(0, state.PlayerBlock));
            int projectedHpLoss = Math.Min(Math.Max(0, state.PlayerHp), hpLeak);
            int excessBlock = Math.Max(0, state.PlayerBlock - incoming);
            bool projectedLethal = !won && (state.PlayerHp <= 0 || hpLeak >= state.PlayerHp);
            double score = enemyHpReduction * weights.EnemyHpReduction + enemyKills * weights.EnemyKill
                + playerHpChange * weights.PlayerHpChange - projectedHpLoss * weights.ProjectedHpLoss
                + Math.Max(0, state.Energy) * weights.RemainingEnergy + excessBlock * weights.ExcessBlock
                + (won ? weights.WinBonus : 0) - (projectedLethal ? weights.DeathPenalty : 0);
            if (!double.IsFinite(score))
                throw new ArgumentException("score_weights produce a non-finite terminal score");
            object breakdown = new
            {
                enemy_hp_reduction = enemyHpReduction, enemy_kills = enemyKills,
                player_hp_change = playerHpChange, projected_incoming_damage = incoming,
                projected_hp_loss = projectedHpLoss, excess_block = excessBlock,
                remaining_energy = state.Energy, combat_won = won, projected_lethal = projectedLethal,
            };
            var endpoint = new Endpoint(position, path, score, breakdown, termination);
            if (best is null || score > best.Score + ScoreTolerance
                || (Math.Abs(score - best.Score) <= ScoreTolerance && path.Count < best.Actions.Count))
                best = endpoint;
            return endpoint;
        }

        /// <summary>Reports heuristic quality, count saturation, coverage, and finite-horizon limits.</summary>
        public object Result(double elapsedMs, bool skipped, string? reason, bool ok = true)
        {
            bool exhaustiveFinished = ok && !skipped && options.Strategy == "exhaustive";
            int supportedCardActions = Initial.LegalActions().Count(action => !IsStop(action));
            bool comparisonAvailable = ok && !skipped && options.MaxDepth > 0 && supportedCardActions > 0;
            return new
            {
                ok, skipped, reason, strategy = options.Strategy,
                comparison_available = comparisonAvailable,
                comparison_reason = comparisonAvailable ? null : "no_supported_card_play_comparison",
                supported_initial_card_actions = supportedCardActions,
                support = Initial.Support,
                score_support = Initial.ScoreSupport,
                search_support_reasons = supportReasons.Order().ToArray(),
                full_legal_coverage = fullCoverage,
                best = skipped || best is null ? null : new
                {
                    actions = best.Actions, score = best.Score, state = best.Position.Snapshot(),
                    score_breakdown = best.Breakdown, termination = best.Termination,
                    score_support = best.Position.ScoreSupport,
                },
                metrics = new
                {
                    nodes = Nodes,
                    terminal_plans = skipped && options.Strategy == "exhaustive" ? CountTerminals : terminals,
                    terminal_count_is_lower_bound = CountTerminals > options.ExhaustiveLimit,
                    elapsed_ms = elapsedMs, count_elapsed_ms = CountElapsedMs,
                    search_elapsed_ms = SearchElapsedMs, count_nodes = countNodes,
                    precount_terminal_plans = options.Strategy == "exhaustive" ? (long?)CountTerminals : null,
                    horizon_terminal_plans = horizonTerminals,
                },
                exact_within_horizon = exhaustiveFinished && fullCoverage && comparisonAvailable,
                optimal_within_supported_horizon = exhaustiveFinished && comparisonAvailable,
                heuristic_score_only = true,
                limits = new
                {
                    max_depth = options.MaxDepth, beam_width = options.BeamWidth,
                    exhaustive_limit = options.ExhaustiveLimit,
                    maximum_configurable_depth = MaximumDepth,
                    horizon = "current_player_turn; max_depth counts non-stop actions",
                    action_domain = "play_card plus stop; potions and other combat actions excluded",
                    optimality_scope = "supported deterministic actions only; score is not win probability",
                    stop_behavior = "end_turn stops projection without executing enemy AI or drawing",
                    score_weights = new
                    {
                        enemy_hp_reduction = options.Weights.EnemyHpReduction,
                        enemy_kill = options.Weights.EnemyKill,
                        player_hp_change = options.Weights.PlayerHpChange,
                        projected_hp_loss = options.Weights.ProjectedHpLoss,
                        remaining_energy = options.Weights.RemainingEnergy,
                        excess_block = options.Weights.ExcessBlock,
                        win_bonus = options.Weights.WinBonus,
                        death_penalty = options.Weights.DeathPenalty,
                    },
                },
            };
        }
    }
}
