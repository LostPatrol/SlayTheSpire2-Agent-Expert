// Reviewed current-turn potion rules, imported only from visible belt slots.
using System.Text.Json;
using System.Text.RegularExpressions;
using Sts2Emulator.Core;
using Sts2Emulator.Core.Effects;

namespace CombatSimulation;

public sealed partial class SimulationPosition
{
    // Amounts match the pinned v0.107.1 public potion descriptions, not live preview damage.
    private static readonly Dictionary<string, PotionRule> PotionRules = new(StringComparer.Ordinal)
    {
        ["BLOCKPOTION"] = new("block", 12),
        ["BLOODPOTION"] = new("heal_percent", 20),
        ["DEXTERITYPOTION"] = new("dexterity", 2),
        ["ENERGYPOTION"] = new("energy", 2),
        ["EXPLOSIVEAMPOULE"] = new("area_damage", 10),
        ["FIREPOTION"] = new("damage", 20, true),
        ["FOCUSPOTION"] = new("focus", 2),
        ["FRUITJUICE"] = new("max_hp", 5),
        ["GHOSTINAJAR"] = new("intangible", 1),
        ["HEARTOFIRON"] = new("plating", 7),
        ["LIQUIDBRONZE"] = new("thorns", 3),
        ["POISONPOTION"] = new("poison", 6, true),
        ["REGENPOTION"] = new("regen", 5),
        ["SHACKLINGPOTION"] = new("shackle", 7),
        ["STARPOTION"] = new("stars", 3),
        ["STRENGTHPOTION"] = new("strength", 2),
        ["VULNERABLEPOTION"] = new("vulnerable", 3, true),
        ["WEAKPOTION"] = new("weak", 3, true),
    };

    // These enemy powers have no additional unpowered-damage hook beyond the reused core path.
    private static readonly HashSet<BuffId> PotionDamagePowers =
    [
        BuffId.Strength, BuffId.Dexterity, BuffId.Focus, BuffId.Vulnerable, BuffId.Weak,
        BuffId.Frail, BuffId.Artifact, BuffId.HardToKill, BuffId.Slippery, BuffId.Thorns,
        BuffId.Intangible, BuffId.Soar, BuffId.Minion, BuffId.Illusion, BuffId.Ritual,
        BuffId.BattlewornDummyTimeLimit, BuffId.Rampart, BuffId.Galvanic,
    ];

    private List<PotionIdentity> _potions = [];

    /// <summary>Retain visible belt identities without inventing unknown potion effects.</summary>
    private void ImportPotions(JsonElement publicState)
    {
        foreach (var source in Items(Get(Get(publicState, "run"), "potions")))
        {
            string id = Text(source, "potion_id");
            bool occupied = Boolean(source, "occupied", id.Length > 0);
            int index = Number(source, "i", -1);
            string? gap = null;
            PotionRules.TryGetValue(Normalize(id), out var rule);
            if (occupied && rule is null) gap = "potion_rule_not_reviewed:" + id;
            if (index < 0 || _potions.Any(potion => potion.Index == index))
            {
                gap = "invalid_or_duplicate_potion_slot";
                for (int previous = 0; previous < _potions.Count; previous++)
                    if (_potions[previous].Index == index) _potions[previous] = _potions[previous] with { Gap = gap };
            }
            if (rule is not null && PotionDescriptionMismatch(Text(source, "description"), rule.Amount))
                gap = "potion_visible_amount_not_imported:" + id;
            int[] targets = Items(Get(source, "targets"))
                .Where(target => target.ValueKind == JsonValueKind.Number && target.TryGetInt32(out _))
                .Select(target => target.GetInt32()).ToArray();
            if (occupied && rule is not null && !rule.Targeted && targets.Length > 0)
                gap = "potion_player_target_selection_not_supported";
            var identity = new PotionIdentity(index, id, occupied,
                Boolean(source, "usable") && !Boolean(source, "is_queued"), targets, rule, gap);
            _potions.Add(identity);
            if (occupied && gap is not null) Support.Note(gap, id);
        }
    }

    /// <summary>Reject changed visible scalars while allowing resource-icon descriptions.</summary>
    private static bool PotionDescriptionMismatch(string description, int expected)
    {
        string readable = Regex.Replace(description, @"\[(?:energy|star):(\d+)\]", "$1");
        readable = Regex.Replace(readable, @"\[[^\]]*\]", "");
        var numbers = Regex.Matches(readable, @"-?\d+(?:\.\d+)?");
        return numbers.Count > 0 && (numbers.Count != 1 || numbers[0].Value != expected.ToString());
    }

    /// <summary>Copy belt occupancy so consuming a potion cannot mutate a sibling branch.</summary>
    private void CopyPotionsTo(SimulationPosition clone) => clone._potions = _potions
        .Select(potion => potion with { Targets = [.. potion.Targets] }).ToList();

    /// <summary>Enumerate explicitly supported potions without adding them to card search.</summary>
    public IReadOnlyList<SimAction> PotionActions()
    {
        if (Stopped || IsTerminal || State.PendingSelection is not null) return [];
        var actions = new List<SimAction>();
        foreach (var potion in _potions.Where(potion => potion.Occupied && potion.Usable))
        {
            IEnumerable<int?> targets = potion.Rule?.Targeted == true
                ? potion.Targets.Select(target => (int?)target) : [null];
            foreach (var target in targets)
            {
                var action = new SimAction("use_potion", TargetIndex: target, PotionIndex: potion.Index);
                string? gap = CheckPotionTransition(action, out _);
                if (gap is null) actions.Add(action);
                else Support.Note($"potion={potion.Index}:{gap}");
            }
        }
        return actions;
    }

    /// <summary>Emit visible slot consumption and per-potion coverage, never random future drops.</summary>
    private object PotionSnapshot() => _potions.Select(potion => new
    {
        i = potion.Index, potion_id = potion.Occupied ? potion.Id : null,
        occupied = potion.Occupied, usable = potion.Occupied && potion.Usable,
        supported = potion.Gap is null && potion.Rule is not null, boundary_reason = potion.Gap,
    }).ToArray();

    /// <summary>Commit a complete private potion probe only after every boundary check passes.</summary>
    private bool ApplyPotion(SimAction action, out string? error)
    {
        error = CheckPotionTransition(action, out var successor);
        if (error is not null || successor is null)
        {
            Support.Note(error ?? "unknown_potion_transition_failure");
            return false;
        }
        int index = _potions.FindIndex(potion => potion.Index == action.PotionIndex);
        if (_potions[index].Rule?.Effect == "shackle")
        {
            for (int enemy = 0; enemy < successor.Enemies.Count; enemy++)
                if (BuffSystem.Get(successor.Enemies[enemy].Buffs, BuffId.TemporaryStrength)
                    > BuffSystem.Get(State.Enemies[enemy].Buffs, BuffId.TemporaryStrength))
                    _enemies[enemy].PowerNames[BuffId.TemporaryStrength] = "SHACKLING_POTION_POWER";
        }
        State = successor;
        _potions[index] = _potions[index] with { Occupied = false, Usable = false };
        _transitionGaps.Clear();
        return true;
    }

    /// <summary>Resolve fixed potion effects atomically and reject missing hooks or hidden draws.</summary>
    private string? CheckPotionTransition(SimAction action, out CombatState? successor)
    {
        successor = null;
        if (Support.Level == "unsupported") return "unsupported_public_combat";
        if (_blockingGaps.Count > 0) return string.Join(";", _blockingGaps);
        if (State.PendingSelection is not null) return "potion_blocked_by_pending_selection";
        if (action.CardHandle is not null || action.CardIndex is not null) return "unexpected_card_identity_for_potion";
        var potion = _potions.Find(potion => potion.Index == action.PotionIndex);
        if (potion is null || !potion.Occupied) return "unknown_or_consumed_potion_slot";
        if (potion.Gap is not null) return potion.Gap;
        if (potion.Rule is not { } rule) return "potion_rule_not_reviewed:" + potion.Id;
        if (!potion.Usable) return "public_potion_not_usable";
        int targetIndex = action.TargetIndex.HasValue ? _enemies.FindIndex(enemy => enemy.Slot == action.TargetIndex) : -1;
        if (rule.Targeted && (targetIndex < 0 || !potion.Targets.Contains(action.TargetIndex!.Value)
            || State.Enemies[targetIndex].Hp <= 0 || !_enemies[targetIndex].Hittable))
            return "invalid_public_potion_target";
        if (!rule.Targeted && action.TargetIndex is not null) return "unexpected_target_for_potion";
        if (rule.Effect == "shackle" && State.Enemies.Select((enemy, index) => (enemy, index)).Any(item =>
            BuffSystem.Get(item.enemy.Buffs, BuffId.TemporaryStrength) != 0
            && (!_enemies[item.index].PowerNames.TryGetValue(BuffId.TemporaryStrength, out string? name)
                || Normalize(name) != "SHACKLINGPOTIONPOWER")))
            return "potion_temporary_strength_subtypes_not_separable";
        bool damagePotion = rule.Effect is "damage" or "area_damage";
        if (damagePotion && State.Relics.Any(relic => relic.DefId == RelicEffects.HandDrill))
            return "potion_hand_drill_damage_hook_not_reviewed";
        var affected = rule.Effect == "damage" ? new[] { State.Enemies[targetIndex] }
            : State.Enemies.Where(enemy => enemy.Hp > 0).ToArray();
        if (damagePotion && affected.Any(enemy => enemy.Buffs.Any(buff => !PotionDamagePowers.Contains(buff.Id))))
            return "potion_enemy_damage_reaction_not_reviewed";

        var clone = State.Clone();
        var initialEnemies = clone.Enemies.ToArray();
        int[] hpBefore = initialEnemies.Select(enemy => enemy.Hp).ToArray();
        var rng = new BoundaryRandom();
        try
        {
            ApplyPotionEffect(clone, rule, targetIndex, rng);
            // Reuse the same death hooks as card plays, including Horn, revival and roster changes.
            if (damagePotion) CombatEngine.HandleEnemyDeaths(clone, hpBefore, rng);
            RelicEffects.ApplyAfterPotionUsed(clone);
            RelicEffects.ApplyAfterPlayerHpChanged(clone);
            if (clone.CardsDrawnThisCombat != State.CardsDrawnThisCombat) return "potion_unknown_draw_boundary";
            if (clone.PendingSelection is not null) return "potion_selection_boundary";
            if (clone.Hand.Count != State.Hand.Count) return "potion_hand_generation_boundary";
            if (clone.Enemies.Any(enemy => !initialEnemies.Contains(enemy))) return "potion_enemy_spawn_boundary";
            if (initialEnemies.Any(enemy => !clone.Enemies.Contains(enemy) && enemy.Hp > 0))
                return "potion_living_enemy_removal_boundary";
            clone.Enemies = [.. initialEnemies];
            successor = clone;
            return null;
        }
        catch (RandomBoundaryException) { return "potion_random_or_shuffle_boundary"; }
    }

    /// <summary>Use reviewed core primitives; unpowered potion damage excludes Strength and Vulnerable.</summary>
    private static void ApplyPotionEffect(CombatState state, PotionRule rule, int targetIndex, Random rng)
    {
        int amount = rule.Amount;
        switch (rule.Effect)
        {
            case "block": CardEffects.GainUnpoweredBlock(state, amount, rng); break;
            case "heal_percent": CardEffects.HealPlayer(state, state.PlayerMaxHp * amount / 100); break;
            case "energy": CardEffects.GainEnergy(state, amount); break;
            case "stars": CardEffects.GainStars(state, amount); break;
            case "max_hp": CardEffects.GainMaxHp(state, amount); break;
            case "damage": CardEffects.DealUnpoweredDamage(state, state.Enemies[targetIndex], amount); break;
            case "area_damage":
                foreach (var enemy in state.Enemies.Where(enemy => enemy.Hp > 0).ToArray())
                    CardEffects.DealUnpoweredDamage(state, enemy, amount);
                break;
            case "strength": RelicEffects.GainPlayerStrength(state, amount); break;
            case "dexterity": BuffSystem.Apply(state.PlayerBuffs, BuffId.Dexterity, amount); break;
            case "focus": BuffSystem.Apply(state.PlayerBuffs, BuffId.Focus, amount); break;
            case "intangible": BuffSystem.Apply(state.PlayerBuffs, BuffId.Intangible, amount); break;
            case "plating": BuffSystem.Apply(state.PlayerBuffs, BuffId.Plating, amount); break;
            case "thorns": BuffSystem.Apply(state.PlayerBuffs, BuffId.Thorns, amount); break;
            case "regen": BuffSystem.Apply(state.PlayerBuffs, BuffId.Regen, amount); break;
            case "shackle":
                foreach (var enemy in state.Enemies.Where(enemy => enemy.Hp > 0))
                {
                    if (BuffSystem.TryConsumeArtifact(enemy.Buffs)) continue;
                    BuffSystem.Apply(enemy.Buffs, BuffId.Strength, -amount);
                    BuffSystem.Apply(enemy.Buffs, BuffId.TemporaryStrength, amount);
                }
                break;
            case "vulnerable":
            case "weak":
            case "poison":
                var target = state.Enemies[targetIndex];
                var power = rule.Effect == "vulnerable" ? BuffId.Vulnerable
                    : rule.Effect == "weak" ? BuffId.Weak : BuffId.Poison;
                int before = BuffSystem.Get(target.Buffs, power);
                BuffSystem.Apply(target.Buffs, power, amount);
                if (power == BuffId.Vulnerable && BuffSystem.Get(target.Buffs, power) > before)
                    CardEffects.DrawCards(state, BuffSystem.Get(state.PlayerBuffs, BuffId.Vicious), rng);
                break;
        }
    }

    /// <summary>Describe one audited immediate effect and its canonical visible amount.</summary>
    private sealed record PotionRule(string Effect, int Amount, bool Targeted = false);

    /// <summary>Keep current public belt slots stable across hypothetical consumption.</summary>
    private sealed record PotionIdentity(int Index, string Id, bool Occupied, bool Usable,
        int[] Targets, PotionRule? Rule, string? Gap);
}
