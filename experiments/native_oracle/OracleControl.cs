// Synthetic-fixture controls and public snapshots for the isolated native oracle.
using System.Text.Json;
using MegaCrit.Sts2.Core.Combat;
using MegaCrit.Sts2.Core.Combat.History.Entries;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Entities.Creatures;
using MegaCrit.Sts2.Core.Entities.Powers;
using MegaCrit.Sts2.Core.Entities.Potions;
using MegaCrit.Sts2.Core.Entities.Players;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Powers;
using MegaCrit.Sts2.Core.MonsterMoves.Intents;
using MegaCrit.Sts2.Core.Runs;

namespace Sts2Headless;

public partial class RunSimulator
{
    // Preserve initial enemy slots even when native death handling removes a creature.
    private List<Creature> _oracleEnemies = [];
    // Legacy dispatch counters stay separate from completed native play history.
    private int _oraclePlayed, _oracleAttacks, _oracleSkills;

    /// <summary>Replace only in-memory piles after a synthetic encounter has settled.</summary>
    public Dictionary<string, object?> OracleSetup(JsonElement setup)
    {
        if (_runState is null) return Error("No synthetic run initialized");
        var player = _runState.Players[0];
        var pcs = player.PlayerCombatState;
        if (pcs is null) return Error("Synthetic encounter not in combat");
        if (setup.TryGetProperty("energy", out var energy)) pcs.Energy = energy.GetInt32();
        // Reuse registered native combat copies; new run cards lack combat IDs after entry.
        var available = pcs.Hand.Cards.Concat(pcs.DrawPile.Cards).Concat(pcs.DiscardPile.Cards)
            .Concat(pcs.ExhaustPile.Cards).ToList();
        if (available.Distinct(ReferenceEqualityComparer.Instance).Count() != available.Count)
            return Error("native_fixture_duplicate_registered_instance");
        var piles = new[] { ("hand", pcs.Hand), ("draw", pcs.DrawPile),
            ("discard", pcs.DiscardPile), ("exhaust", pcs.ExhaustPile) };
        var assignments = new List<(CardModel Card, int Pile, bool Upgrade)>();
        for (var pileIndex = 0; pileIndex < piles.Length; pileIndex++)
        {
            var (name, _) = piles[pileIndex];
            if (!setup.TryGetProperty(name, out var cards)) continue;
            foreach (var spec in cards.EnumerateArray())
            {
                string id = spec.ValueKind == JsonValueKind.String ? spec.GetString()!
                    : spec.GetProperty("card_id").GetString()!;
                var card = available.FirstOrDefault(card => card.Id.Entry == id);
                if (card is null) return Error($"Fixture card not registered in native combat: {id}");
                available.Remove(card);
                assignments.Add((card, pileIndex, spec.ValueKind == JsonValueKind.Object
                    && spec.TryGetProperty("upgraded", out var up) && up.GetBoolean()));
            }
        }
        // Omitted fixture piles mean empty, not an alias of a card moved from the initial hand/draw.
        foreach (var (_, pile) in piles) pile.Clear(silent: true);
        foreach (var (card, pileIndex, upgrade) in assignments)
        {
            if (upgrade && !card.IsUpgraded)
            {
                card.UpgradeInternal();
                card.FinalizeUpgradeInternal();
            }
            piles[pileIndex].Item2.AddInternal(card, silent: true);
        }
        var allocated = piles.SelectMany(pair => pair.Item2.Cards).ToArray();
        if (allocated.Length != assignments.Count
            || allocated.Distinct(ReferenceEqualityComparer.Instance).Count() != allocated.Length)
            return Error("native_fixture_pile_allocation_not_unique");
        _oracleEnemies = CombatManager.Instance.DebugOnlyGetState()!.Enemies.ToList();
        _oraclePlayed = _oracleAttacks = _oracleSkills = 0;
        return OracleSnapshot();
    }

    /// <summary>Use native action handlers and preserve pending player selections.</summary>
    public Dictionary<string, object?> OracleStep(JsonElement action)
    {
        string type = action.GetProperty("type").GetString()!;
        if (type == "end_turn") return Error("native_end_turn_not_validated_as_oracle_evidence");
        var args = new Dictionary<string, object?>();
        CardType? playedType = null;
        if (type == "play_card")
        {
            int index = action.GetProperty("card_index").GetInt32();
            args["card_index"] = index;
            playedType = _runState!.Players[0].PlayerCombatState!.Hand.Cards[index].Type;
        }
        else if (type == "use_potion")
        {
            var player = _runState!.Players[0];
            var slots = GetBackingList<PotionModel>(player, "_potionSlots")!;
            int slot = action.GetProperty("option_index").GetInt32();
            if (slot < 0 || slot >= slots.Count || slots[slot] is null) return Error("Empty native potion slot");
            args["potion_index"] = player.Potions.ToList().IndexOf(slots[slot]);
        }
        else if (type == "select_card")
        {
            args["indices"] = action.TryGetProperty("choice_indices", out var choices)
                ? string.Join(",", choices.EnumerateArray().Select(choice => choice.GetInt32()))
                : action.GetProperty("choice_index").GetInt32().ToString();
            type = "select_cards";
        }
        else if (type != "end_turn") return Error("Unsupported oracle action type");
        if (action.TryGetProperty("target_index", out var target))
        {
            var slot = target.GetInt32();
            if (slot < 0 || slot >= _oracleEnemies.Count || !_oracleEnemies[slot].IsAlive)
                return Error("native_target_slot_not_alive");
            var alive = CombatManager.Instance.DebugOnlyGetState()!.Enemies.Where(enemy => enemy.IsAlive).ToList();
            var nativeIndex = alive.FindIndex(enemy => ReferenceEquals(enemy, _oracleEnemies[slot]));
            if (nativeIndex < 0) return Error("native_target_slot_not_present");
            args["target_index"] = nativeIndex;
        }
        var result = ExecuteAction(type, args);
        if (result.GetValueOrDefault("type") as string == "error") return result;
        if (playedType is { } kind)
        {
            _oraclePlayed++;
            if (kind == CardType.Attack) _oracleAttacks++;
            if (kind == CardType.Skill) _oracleSkills++;
        }
        return OracleSnapshot();
    }

    /// <summary>Export visible quantities only; actual draw order and RNG stay inside the test process.</summary>
    public Dictionary<string, object?> OracleSnapshot()
    {
        var player = _runState!.Players[0];
        var pcs = player.PlayerCombatState!;
        var state = CombatManager.Instance.DebugOnlyGetState();
        foreach (var enemy in state?.Enemies ?? [])
            if (!_oracleEnemies.Any(prior => ReferenceEquals(prior, enemy))) _oracleEnemies.Add(enemy);
        var creatures = _oracleEnemies;
        var aliveSlots = creatures.Select((enemy, index) => (enemy, index))
            .Where(pair => pair.enemy.IsAlive && pair.enemy.IsHittable).Select(pair => pair.index).ToArray();
        var hand = pcs.Hand.Cards.Select((card, index) => OracleCard(card, index, aliveSlots)).ToArray();
        var (nativeHistory, publicHistory) = OracleHistory(player, state);
        var powerDetails = OraclePowerDetails(player.Creature);
        var orbs = pcs.OrbQueue.Orbs.Select((orb, index) => new { slot_index = index, orb_id = orb.Id.Entry,
            passive_value = orb.PassiveVal, evoke_value = orb.EvokeVal, is_front = index == 0 }).ToArray();
        var pets = pcs.Pets.Select((pet, index) => new { i = index, pet_id = pet.Monster?.Id.Entry,
            current_hp = pet.CurrentHp, max_hp = pet.MaxHp, block = pet.Block,
            power_details = OraclePowerDetails(pet) }).ToArray();
        var publicPlayer = new
        {
            hp = $"{player.Creature.CurrentHp}/{player.Creature.MaxHp}", block = player.Creature.Block,
            energy = pcs.Energy, stars = pcs.Stars, powers = OraclePowers(player.Creature),
            power_details = powerDetails, orbs, orb_details = orbs, pets, pet_details = pets,
            focus = player.Creature.GetPowerAmount<FocusPower>(), base_orb_slots = player.BaseOrbSlotCount,
            orb_capacity = pcs.OrbQueue.Capacity, empty_orb_slots = Math.Max(0, pcs.OrbQueue.Capacity - orbs.Length),
            card_play_counters_scope = "manual_oracle_dispatches_only",
            cards_played_this_turn = _oraclePlayed, attacks_played_this_turn = _oracleAttacks,
            skills_played_this_turn = _oracleSkills,
        };
        var enemies = creatures.Select((enemy, index) => new
        {
            i = index, enemy_id = enemy.Monster!.Id.Entry, hp = $"{enemy.CurrentHp}/{enemy.MaxHp}",
            block = enemy.Block, powers = OraclePowers(enemy), power_details = OraclePowerDetails(enemy),
            alive = enemy.IsAlive, hittable = enemy.IsHittable, intents = OracleIntents(enemy, player),
        }).ToArray();
        var potions = GetBackingList<PotionModel>(player, "_potionSlots")!.Select((potion, index) => new
        {
            i = index, potion_id = potion?.Id.Entry, occupied = potion is not null,
            usable = potion is not null && potion.PassesCustomUsabilityCheck && !potion.IsQueued
                && player.CanRemovePotions && potion.Usage is PotionUsage.AnyTime or PotionUsage.CombatOnly,
            description = "", target = potion?.TargetType == TargetType.AnyEnemy ? "enemies" : null,
            targets = potion?.TargetType == TargetType.AnyEnemy ? aliveSlots : Array.Empty<int>(),
        }).ToArray();
        object? pending = _cardSelector.HasPending ? new
        {
            kind = "select_card",
            candidates = _cardSelector.PendingOptions!.Select((card, index) =>
            { var row = OracleCard(card, index, aliveSlots); row["choice_index"] = index; return row; }).ToArray(),
            min_select = _cardSelector.PendingMinSelect, max_select = _cardSelector.PendingMaxSelect,
        } : null;
        if (pending is null && RunManager.Instance.ActionExecutor.IsRunning)
            return Error("native_action_executor_not_settled");
        return new Dictionary<string, object?>
        {
            ["ok"] = true, ["schema_version"] = 2, ["pending_choice"] = pending,
            ["capabilities"] = new { scope = "native_player_turn_public_observations", end_turn_validated = false,
                stable_enemy_slots = true, native_completed_play_history = true,
                current_card_previews = true, unordered_pile_descriptors = true },
            ["public_state"] = new
            {
                observation_schema_version = 2, screen = "COMBAT", supported = true, turn = state?.RoundNumber ?? 1,
                public_history = publicHistory,
                run = new { character = player.Character.Id.Entry, ascension = 0, hp = publicPlayer.hp,
                    gold = player.Gold, max_energy = pcs.MaxEnergy,
                    relic_ids = player.Relics.Select(relic => relic.Id.Entry).ToArray(),
                    relic_stacks = player.Relics.Select(relic => relic.ShowCounter ? OracleInt(() => relic.DisplayAmount) : null).ToArray(), potions },
                combat = new
                {
                    player = publicPlayer, hand, enemies, public_card_play_history = nativeHistory,
                    draw = OraclePile(pcs.DrawPile.Cards), discard = OraclePile(pcs.DiscardPile.Cards),
                    exhaust = OraclePile(pcs.ExhaustPile.Cards),
                    action_readiness = new { can_use_combat_actions = pending is null, actions_settled = pending is null,
                        snapshot_stable = pending is null, modal_open = pending is not null, is_paused = false },
                },
            },
            ["endpoint"] = new
            {
                schema_version = 2,
                player = new { hp = new { current = player.Creature.CurrentHp, max = player.Creature.MaxHp },
                    block = player.Creature.Block, energy = pcs.Energy, stars = pcs.Stars, powers = publicPlayer.powers,
                    power_details = powerDetails, orbs, pets },
                enemies = creatures.Select((enemy, index) => new { i = index, enemy_id = enemy.Monster!.Id.Entry,
                    hp = new { current = enemy.CurrentHp, max = enemy.MaxHp }, block = enemy.Block,
                    powers = OraclePowers(enemy), power_details = OraclePowerDetails(enemy),
                    alive = enemy.IsAlive, hittable = enemy.IsHittable }).ToArray(),
                hand,
                piles = new { draw = pcs.DrawPile.Cards.Count, discard = pcs.DiscardPile.Cards.Count, exhaust = pcs.ExhaustPile.Cards.Count },
                pile_cards = new { draw = OraclePile(pcs.DrawPile.Cards), discard = OraclePile(pcs.DiscardPile.Cards),
                    exhaust = OraclePile(pcs.ExhaustPile.Cards) },
                potions, pending_choice = pending, public_card_play_history = nativeHistory, public_history = publicHistory,
            },
        };
    }

    /// <summary>Read current public instance properties using the same native preview API as the bridge.</summary>
    private static Dictionary<string, object?> OracleCard(CardModel card, int index, int[] targets)
    {
        var energy = OracleInt(() => card.EnergyCost.GetWithModifiers(CostModifiers.All));
        var stars = OracleInt(() => Math.Max(0, card.GetStarCostWithModifiers()));
        var keywords = card.Keywords.Where(keyword => keyword != CardKeyword.None)
            .Select(keyword => keyword.ToString()).Distinct().Order(StringComparer.Ordinal).ToArray();
        object? dynamicValues = null;
        try
        {
            var preview = card.DynamicVars.Clone(card);
            card.UpdateDynamicVarPreview(CardPreviewMode.Normal, card.CurrentTarget, preview);
            dynamicValues = preview.Values.Select(value => new { name = value.Name,
                base_value = (int)value.BaseValue, current_value = (int)value.PreviewValue,
                enchanted_value = (int)value.EnchantedValue }).OrderBy(value => value.name, StringComparer.Ordinal).ToArray();
        }
        catch (Exception) { /* Null explicitly marks unavailable preview data in the headless environment. */ }
        return new Dictionary<string, object?>
        {
            ["i"] = index, ["card_id"] = card.Id.Entry, ["upgraded"] = card.IsUpgraded,
            ["line"] = $"{card.Id.Entry}{(card.IsUpgraded ? "+" : "")} [{energy}]",
            ["cost"] = energy, ["energy_cost"] = energy, ["star_cost"] = stars,
            ["costs_x"] = card.EnergyCost.CostsX, ["star_costs_x"] = card.HasStarCostX,
            ["card_type"] = card.Type.ToString(), ["rarity"] = card.Rarity.ToString(),
            ["playable"] = card.CanPlay(out _, out _),
            ["target"] = card.TargetType == TargetType.AnyEnemy ? "enemies" : null,
            ["targets"] = card.TargetType == TargetType.AnyEnemy ? targets : Array.Empty<int>(),
            ["mods"] = Array.Empty<string>(), ["keywords"] = keywords, ["effective_keywords"] = keywords,
            ["keyword_state_complete"] = true,
            ["affliction"] = card.Affliction is null ? null : new { affliction_id = card.Affliction.Id.Entry, amount = card.Affliction.Amount },
            ["enchantment"] = card.Enchantment is null ? null : new { enchantment_id = card.Enchantment.Id.Entry,
                amount = card.Enchantment.Amount, display_amount = OracleInt(() => card.Enchantment.DisplayAmount) },
            ["dynamic_values"] = dynamicValues,
        };
    }

    /// <summary>Sort visible descriptors so test-only materialized pile order cannot leak.</summary>
    private static object[] OraclePile(IEnumerable<CardModel> cards) => cards.Select(card =>
        {
            var row = OracleCard(card, 0, []);
            row.Remove("i"); row.Remove("playable"); row.Remove("targets"); row.Remove("target");
            row["card_ids"] = new[] { card.Id.Entry }; row["card_count"] = 1;
            return row;
        }).OrderBy(card => JsonSerializer.Serialize(card), StringComparer.Ordinal).Cast<object>().ToArray();

    /// <summary>Keep failed optional public integer reads unknown rather than substituting zero.</summary>
    private static int? OracleInt(Func<int> read)
    {
        try { return read(); }
        catch (Exception) { return null; }
    }

    /// <summary>Expose public power amounts and visible counters separately.</summary>
    private static object[] OraclePowerDetails(Creature creature) => creature.Powers.Select((power, index) => new
    {
        index, power_id = power.Id.Entry, amount = power.Amount,
        display_amount = OracleInt(() => power.DisplayAmount), is_debuff = power.TypeForCurrentAmount == PowerType.Debuff,
    }).Cast<object>().ToArray();

    /// <summary>Project visible intent numbers without exporting native move identity or history.</summary>
    private static object[] OracleIntents(Creature enemy, Player player)
    {
        var targets = new[] { player.Creature };
        return enemy.Monster?.NextMove?.Intents.Select((intent, index) => new
        {
            index, intent_type = intent.IntentType.ToString(),
            damage = intent is AttackIntent attack ? OracleInt(() => attack.GetSingleDamage(targets, enemy)) : null,
            hits = intent is AttackIntent repeated ? OracleInt(() => Math.Max(1, repeated.Repeats)) : null,
            total_damage = intent is AttackIntent total ? OracleInt(() => total.GetTotalDamage(targets, enemy)) : null,
            status_card_count = intent is StatusIntent status ? OracleInt(() => status.CardCount) : null,
        }).Cast<object>().ToArray() ?? [];
    }

    /// <summary>Copy completed local plays, including replay/auto plays, without hidden future state.</summary>
    private static (object Native, object Derived) OracleHistory(Player player, CombatState? state)
    {
        var rows = new List<OraclePlay>();
        var available = state is not null && CombatManager.Instance.History is not null;
        var complete = available;
        if (available)
        {
            foreach (var entry in CombatManager.Instance.History.Entries.ToArray())
            {
                if (entry is not CardPlayFinishedEntry finished) continue;
                var play = finished.CardPlay;
                if (play?.Card?.Owner is null) { complete = false; continue; }
                if (!ReferenceEquals(play.Card.Owner, player)) continue;
                rows.Add(new OraclePlay(rows.Count, play.Card.Id.Entry, play.Card.Type.ToString(),
                    play.IsAutoPlay, play.IsFirstInSeries, entry.HappenedThisTurn(state!)));
            }
        }
        var current = rows.Where(row => row.this_turn).ToArray();
        var manual = current.Where(row => !row.auto_played && row.first_in_series).ToArray();
        return (new { available, complete, scope = "current_combat", source = "native_completed_local_card_plays", plays = rows },
            new { schema_version = 1, history_complete = complete, complete_from_start = complete,
                source = "native_completed_local_card_plays",
                total_card_plays_this_turn = complete ? (int?)current.Length : null,
                total_attack_plays_this_turn = complete ? (int?)current.Count(row => row.card_type == "Attack") : null,
                total_skill_plays_this_turn = complete ? (int?)current.Count(row => row.card_type == "Skill") : null,
                manual_card_plays_this_turn = complete ? (int?)manual.Length : null,
                manual_attack_plays_this_turn = complete ? (int?)manual.Count(row => row.card_type == "Attack") : null,
                manual_skill_plays_this_turn = complete ? (int?)manual.Count(row => row.card_type == "Skill") : null,
                permafrost_first_power_used = complete ? (bool?)rows.Any(row => row.card_type == "Power") : null });
    }

    // The native history shape matches the production bridge's reviewed public contract.
    private sealed record OraclePlay(int sequence, string card_id, string card_type,
        bool auto_played, bool first_in_series, bool this_turn);

    /// <summary>Use native power identity and current debuff classification.</summary>
    private static string[] OraclePowers(Creature creature) => creature.Powers.Select(power =>
        $"{power.Id.Entry} {power.Amount}" + (power.TypeForCurrentAmount == PowerType.Debuff ? " [debuff]" : "")).ToArray();
}
