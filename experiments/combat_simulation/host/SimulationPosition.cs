// Adapts reviewed public observations to a deterministic, current-player-turn sandbox.
using System.Reflection;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Sts2Emulator.Core;
using Sts2Emulator.Core.Effects;
using Sts2Emulator.GeneratedData;

namespace CombatSimulation;

/// <summary>A stable input-hand identity and public enemy slot, independent of list shifts.</summary>
public sealed record SimAction(string Type, string? CardHandle = null, int? CardIndex = null,
    int? TargetIndex = null, int? PotionIndex = null, int? ChoiceIndex = null, string? CardId = null);

/// <summary>Reports the supported deterministic subgraph rather than full-game fidelity.</summary>
public sealed class SupportReport
{
    public string Level { get; set; } = "exact";
    public List<string> Reasons { get; set; } = [];
    public List<string> UnknownIds { get; set; } = [];

    /// <summary>Add a distinct gap without rejecting unrelated supported actions.</summary>
    public void Note(string reason, string? unknownId = null)
    {
        if (!Reasons.Contains(reason)) Reasons.Add(reason);
        if (unknownId is not null && !UnknownIds.Contains(unknownId)) UnknownIds.Add(unknownId);
        if (Level != "unsupported") Level = "partial";
    }

    /// <summary>Copy mutable diagnostics for a search branch.</summary>
    public SupportReport Clone() => new() { Level = Level, Reasons = [.. Reasons], UnknownIds = [.. UnknownIds] };
}

/// <summary>Maintains public identities alongside the unchanged upstream combat state.</summary>
public sealed partial class SimulationPosition
{
    // Only rules reviewed for this experiment may enter the deterministic action graph.
    private static readonly HashSet<string> ReviewedCards = new(StringComparer.Ordinal)
    {
        "StrikeIronclad", "StrikeSilent", "StrikeDefect", "StrikeRegent", "StrikeNecrobinder",
        "DefendIronclad", "DefendSilent", "DefendDefect", "DefendRegent", "DefendNecrobinder",
        "Anger", "Armaments", "Bash", "Bludgeon", "BurningPact", "DemonForm", "Dismantle",
        "FeelNoPain", "Fisticuffs", "FranticEscape", "Inflame", "IronWave", "Pillage",
        "PommelStrike", "Rampage", "SetupStrike", "ShrugItOff", "Slimed", "Taunt",
        "Thunderclap", "Toxic", "Uppercut", "Vicious", "Whirlwind", "Wound",
        "TwinStrike", "BodySlam", "BloodWall", "Bloodletting", "Break", "Breakthrough",
        "Bully", "Conflagration", "FightMe", "FlameBarrier", "Impervious", "Rage",
        "Colossus", "Corruption", "Cruelty", "DarkEmbrace", "Dominate", "DemonicShield",
        "Hemokinesis", "Metallicize", "NotYet", "OneTwoPunch", "Barricade", "ShrugItOff",
        "Tremble", "TrueGrit", "Unrelenting", "UltimateDefend", "BattleTrance", "Offering",
        "Exterminate", "HowlFromBeyond", "Pyre", "Juggling",
    };

    // These powers need no unobserved history for current-player-turn effects.
    private static readonly HashSet<string> ReviewedPowers = new(StringComparer.Ordinal)
    {
        "Strength", "Dexterity", "Focus", "Vulnerable", "Weak", "Frail", "Artifact",
        "Burrowed", "Constrict", "DemonForm", "HardToKill", "Imbalanced", "Plating",
        "Sandpit", "SetupStrikePower", "TemporaryStrength", "Shrink", "Slippery", "Slumber",
        "Tainted", "Territorial", "Vicious", "VitalSpark", "Poison", "Thorns", "Vigor",
        "Rage", "FeelNoPain", "DarkEmbrace", "Barricade", "Corruption", "Cruelty",
        "FlameBarrier", "Metallicize", "Colossus", "NoDraw", "NoBlock", "NoEnergyGain",
        "OneTwoPunch", "FreeAttackPower", "FreeSkillPower", "FreePowerPower", "Buffer",
        "Intangible", "Juggernaut", "RupturePower", "Inferno", "InfernoSelfDamage", "DemonForm",
        "CrueltyPower", "PyrePower", "MindRot", "Soar", "Tender", "Juggling",
        "Ritual", "BattlewornDummyTimeLimit", "Rampart", "Minion", "Illusion", "Galvanic", "Regen",
    };

    // These relics' relevant counters are either public or irrelevant inside this horizon.
    private static readonly HashSet<string> ReviewedRelics = new(StringComparer.Ordinal)
    {
        "BurningBlood", "Gorget", "MoltenEgg", "OrnamentalFan", "Vajra", "YummyCookie",
        "PaperPhrog", "PaperKrane", "StrikeDummy", "TheBoot", "Kunai", "Shuriken",
        "LetterOpener", "GremlinHorn", "ChemicalX", "Akabeko", "Anchor", "BagOfMarbles",
        "BagOfPreparation", "PreservedInsect", "Fiddle", "HandDrill", "OddlySmoothStone",
        // These effects do not fire inside the imported current-player-turn interval.
        "VenerableTeaSet", "RedMask", "PumpkinCandle", "MercuryHourglass", "JuzuBracelet",
        "Mango", "LastingCandy", "Bellows", "MeatOnTheBone",
        "PaelsFlesh", "HornCleat", "RegalPillow",
        // Its public run counter is imported below; triggering a draw remains a boundary.
        "IronClub",
        // Its fifth-card condition uses the public cards_played_this_turn counter below.
        "BrilliantScarf",
        // Non-Power plays are independent of its unknown first-Power latch.
        "Permafrost",
    };

    // Upstream generated enemy data exposes Get but not an iterator; read its own table once.
    private static readonly Dictionary<string, int> EnemyIds = ((EnemyDef[])typeof(Enemies)
        .GetField("_all", BindingFlags.NonPublic | BindingFlags.Static)!.GetValue(null)!)
        .ToDictionary(x => Normalize(x.Name), x => x.Id);

    // Public aliases include subclasses whose current-turn effect is an expiration marker.
    private static readonly Dictionary<string, BuffId> PowerIds = Enum.GetValues<BuffId>()
        .ToDictionary(x => Normalize(x.ToString()).Replace("POWER", "", StringComparison.Ordinal), x => x);

    // Canonical game slugs are authoritative; displayed dynamic numbers are never base damage.
    private static readonly Dictionary<string, CardDef> CardIds = Cards.All.ToArray()
        .ToDictionary(x => Normalize(x.Entry.Length > 0 ? x.Entry : x.Name), x => x);

    // Relic IDs in the public contract are slugs, whereas the upstream table names are PascalCase.
    private static readonly Dictionary<string, RelicDef> RelicIds = Relics.All.ToArray()
        .ToDictionary(x => Normalize(x.Name), x => x);

    // The public debuff label follows model type; it is broader than the core's Artifact list.
    private static readonly Dictionary<string, bool> PowerBuffTypes = ((PowerDef[])typeof(Powers)
        .GetField("_all", BindingFlags.NonPublic | BindingFlags.Static)!.GetValue(null)!)
        .ToDictionary(x => Normalize(x.Name), x => x.IsBuff);

    private List<HandIdentity> _hand = [];
    private List<EnemyIdentity> _enemies = [];
    private Dictionary<BuffId, string> _playerPowerNames = [];
    private List<string> _playerPassthrough = [];
    private List<string> _blockingGaps = [];
    private List<object> _previewDiagnostics = [];
    private readonly Dictionary<string, string?> _transitionGaps = [];
    private int _seed;
    // Juggling's third-attack trigger cannot use an invented zero counter.
    private bool _attackCountObserved;

    public CombatState State { get; private set; } = new();
    public SupportReport Support { get; private set; } = new();
    public bool Stopped { get; private set; }
    public bool IsTerminal => State.PlayerHp <= 0 || !State.Enemies.Any(enemy =>
        BuffSystem.Get(enemy.Buffs, BuffId.Minion) <= 0
        && BuffSystem.Get(enemy.Buffs, BuffId.Illusion) <= 0
        && (enemy.Hp > 0 || BuffSystem.Get(enemy.Buffs, BuffId.Adaptable) > 0));
    // Surviving secondary enemies cannot hold combat open after their primary dies.
    public bool IsCombatWon => State.PlayerHp > 0 && IsTerminal;

    /// <summary>Current announced attack damage before block, with no future enemy AI.</summary>
    public int ProjectedIncomingDamage => IsTerminal ? 0 : State.Enemies.Select((enemy, index) => (enemy, index))
        .Where(x => x.enemy.Hp > 0 && !BuffSystem.Has(x.enemy.Buffs, BuffId.Stunned))
        .Sum(x => x.enemy.CurrentIntent.Type == IntentType.Attack || x.enemy.CurrentIntent.CarriesDamage
            ? x.enemy.CurrentIntent.AnnouncedDamage(x.enemy.Buffs, State.PlayerBuffs, RelicEffects.WeakMultiplierDeltaAgainstPlayer(State))
                + _enemies[x.index].AdditionalAttacks.Sum(intent => intent.AnnouncedDamage(
                    x.enemy.Buffs, State.PlayerBuffs, RelicEffects.WeakMultiplierDeltaAgainstPlayer(State)))
            : 0);

    /// <summary>Keep heuristic ending-turn coverage distinct from immediate action fidelity.</summary>
    public object ScoreSupport
    {
        get
        {
            var reasons = new List<string>
            {
                "sum_of_current_declared_attacks_only_not_an_executed_enemy_turn",
                "end_turn_card_power_relic_hooks_and_enemy_secondary_effects_not_resolved",
            };
            foreach (var reason in Support.Reasons.Where(x => x.Contains("intent_damage_", StringComparison.Ordinal)))
                reasons.Add(reason);
            foreach (var buff in State.PlayerBuffs)
            {
                if (buff.Id is BuffId.Poison or BuffId.Burn or BuffId.Constrict or BuffId.Doom
                    or BuffId.InfernoSelfDamage or BuffId.TheBombPower)
                    reasons.Add($"player:{ToSlug(buff.Id.ToString())}:{buff.Magnitude}:non_attack_damage_or_lethal_effect_not_scored");
                if (buff.Id is BuffId.Metallicize or BuffId.Plating or BuffId.Regen or BuffId.FlameBarrier
                    or BuffId.TemporaryStrength or BuffId.PyrePower or BuffId.MindRot)
                    reasons.Add($"player:{ToSlug(buff.Id.ToString())}:{buff.Magnitude}:turn_end_gain_expiration_or_retaliation_not_scored");
                if (buff.Id == BuffId.Buffer)
                    reasons.Add($"player:BUFFER_POWER:{buff.Magnitude}:per_hit_damage_prevention_not_scored");
                if (buff.Id == BuffId.Tender)
                    reasons.Add("player:TENDER_POWER:turn_end_strength_and_dexterity_restoration_not_scored");
            }
            if (_playerPassthrough.Any(power => power.StartsWith("CRIMSON_MANTLE_POWER ", StringComparison.Ordinal)))
                reasons.Add("player:CRIMSON_MANTLE_POWER:next_turn_hp_loss_and_block_not_scored");
            foreach (var (enemy, index) in State.Enemies.Select((enemy, index) => (enemy, index)))
            {
                if (enemy.Hp <= 0 && BuffSystem.Has(enemy.Buffs, BuffId.Illusion) && !IsCombatWon)
                    reasons.Add($"enemy_slot={_enemies[index].Slot}:ILLUSION_POWER:future_revive_not_scored");
                if (enemy.Hp <= 0) continue;
                if (_enemies[index].HasNonAttackIntent && !BuffSystem.Has(enemy.Buffs, BuffId.Stunned))
                    reasons.Add($"enemy_slot={_enemies[index].Slot}:visible_non_attack_intent_effect_not_scored");
                foreach (var buff in enemy.Buffs)
                {
                    if (buff.Id is BuffId.Sandpit or BuffId.Constrict or BuffId.Doom or BuffId.Poison
                        or BuffId.TheBombPower)
                        reasons.Add($"enemy_slot={_enemies[index].Slot}:{ToSlug(buff.Id.ToString())}:{buff.Magnitude}:countdown_status_damage_or_special_lethal_not_scored");
                    if (buff.Id is BuffId.Ritual or BuffId.BattlewornDummyTimeLimit or BuffId.Rampart)
                        reasons.Add($"enemy_slot={_enemies[index].Slot}:{ToSlug(buff.Id.ToString())}:future_side_turn_trigger_not_scored");
                }
            }
            for (int i = 0; i < State.Hand.Count; i++)
            {
                if (Cards.Get(State.Hand[i].DefId).TurnEndInHand)
                    reasons.Add($"hand={_hand[i].Handle}:{_hand[i].CardId}:turn_end_in_hand_effect_not_scored");
            }
            if (State.ExhaustPile.Any(card => Cards.Get(card.DefId).Name == "HowlFromBeyond"))
                reasons.Add("exhaust:HOWL_FROM_BEYOND:turn_end_auto_play_not_scored");
            return new
            {
                level = "partial", reasons,
                included = "public_current_declared_attacks_minus_current_block_with_visible_modifiers_and_confirmed_stuns",
                meaning = "transparent_tactical_heuristic_not_end_turn_hp_prediction_or_win_probability",
            };
        }
    }

    /// <summary>Import only the supplied public document, never actual seeds or pile order.</summary>
    public static SimulationPosition Import(JsonElement publicState, int seed)
    {
        var result = new SimulationPosition { _seed = seed };
        var combat = Get(publicState, "combat");
        if (combat.ValueKind != JsonValueKind.Object)
        {
            result.Support.Level = "unsupported";
            result.Support.Reasons.Add("missing_public_combat");
            return result;
        }

        var player = Get(combat, "player");
        var run = Get(publicState, "run");
        var hp = ReadHp(Get(player, "hp"));
        result.State = new CombatState
        {
            PlayerHp = hp.Current, PlayerMaxHp = hp.Max,
            PlayerBlock = Number(player, "block"), Energy = Number(player, "energy"),
            Stars = Number(player, "stars"), MaxEnergy = Number(run, "max_energy", 3),
            PlayerGold = Number(run, "gold"), AscensionLevel = Number(run, "ascension"),
            Turn = Number(publicState, "turn", 1), PlayerTurn = true,
            CardsPlayedThisTurn = Number(player, "cards_played_this_turn"),
            AttackCardsPlayedThisTurn = Number(player, "attacks_played_this_turn"),
            SkillCardsPlayedThisTurn = Number(player, "skills_played_this_turn"),
            WhisperingEarringSpent = true,
        };
        result.State.CardPlaysThisTurn = result.State.CardsPlayedThisTurn;
        var attackCount = Get(player, "attacks_played_this_turn");
        result._attackCountObserved = attackCount.ValueKind == JsonValueKind.Number
            && attackCount.TryGetInt32(out int attacks) && attacks >= 0;
        result.State.AttackOrSkillCardsPlayedThisTurn = result.State.AttackCardsPlayedThisTurn + result.State.SkillCardsPlayedThisTurn;
        result.ReadPowers(Get(player, "powers"), result.State.PlayerBuffs, result._playerPowerNames, result._playerPassthrough, "player");
        result.ReadRelics(run, player);

        foreach (var card in Items(Get(combat, "hand"))) result.ReadHand(card);
        result.ReadPile(Get(combat, "draw"), result.State.DrawPile, "draw");
        result.ReadPile(Get(combat, "discard"), result.State.DiscardPile, "discard");
        result.ReadPile(Get(combat, "exhaust"), result.State.ExhaustPile, "exhaust");
        foreach (var enemy in Items(Get(combat, "enemies"))) result.ReadEnemy(enemy);
        result.ImportPotions(publicState);

        if (Items(Get(player, "orbs")).Any() || Items(Get(player, "pets")).Any())
        {
            result.Support.Note("orbs_or_pets_not_imported");
            result._blockingGaps.Add("orbs_or_pets_not_imported");
        }

        foreach (var hand in result._hand)
        {
            if (hand.InitialGap is not null) result.Support.Note(hand.InitialGap, hand.CardId);
        }
        // Probe one-step trigger boundaries on private copies; no branch state is exposed.
        _ = result.LegalActions();
        return result;
    }

    /// <summary>Clone combat data and public identities without sharing mutable branch state.</summary>
    public SimulationPosition Clone()
    {
        var clone = new SimulationPosition
        {
        State = State.Clone(), Support = Support.Clone(), Stopped = Stopped, _seed = _seed,
        _attackCountObserved = _attackCountObserved,
        _revealedCards = _revealedCards,
        _hand = [.. _hand], _enemies = _enemies.Select(x => x.Copy()).ToList(),
        _playerPowerNames = new(_playerPowerNames), _playerPassthrough = [.. _playerPassthrough],
        _blockingGaps = [.. _blockingGaps], _previewDiagnostics = [.. _previewDiagnostics],
        };
        CopyPotionsTo(clone);
        return clone;
    }

    /// <summary>Enumerate every supported card instance and target, plus the explicit stop.</summary>
    public IReadOnlyList<SimAction> LegalActions()
    {
        if (Stopped || IsTerminal || Support.Level == "unsupported") return [];
        var actions = new List<SimAction>();
        for (int index = 0; index < State.Hand.Count; index++)
        {
            var identity = _hand[index];
            var def = Cards.Get(State.Hand[index].DefId);
            if (!CombatEngine.CanPlayFromHand(State, State.Hand[index])) continue;
            IEnumerable<int?> targets = def.Target == CardTarget.AnyEnemy
                ? State.Enemies.Select((enemy, i) => (enemy, i))
                    .Where(x => x.enemy.Hp > 0 && _enemies[x.i].Hittable)
                    .Select(x => (int?)_enemies[x.i].Slot)
                : [null];
            foreach (var target in targets)
            {
                var action = new SimAction("play_card", identity.Handle, identity.OriginalIndex, target);
                var key = ActionKey(action);
                if (!_transitionGaps.TryGetValue(key, out var gap))
                {
                    gap = CheckTransition(action, out _);
                    _transitionGaps[key] = gap;
                }
                if (gap is null) actions.Add(action);
                else Support.Note($"{identity.Handle}:{identity.CardId}:{gap}");
            }
        }
        actions.Add(new SimAction("end_turn"));
        return actions;
    }

    /// <summary>Apply a supported action atomically; rejected private probes never escape.</summary>
    public bool Apply(SimAction action, out string? error)
    {
        error = null;
        if (Stopped || IsTerminal) { error = "position_already_stopped_or_terminal"; return false; }
        if (action.Type == "use_potion") return ApplyPotion(action, out error);
        if (action.Type == "end_turn") { Stopped = true; return true; }
        error = CheckTransition(action, out var successor);
        if (error is not null || successor is null)
        {
            Support.Note(error ?? "unknown_transition_failure");
            return false;
        }
        int index = FindHand(action);
        _hand.RemoveAt(index);
        State = successor;
        _transitionGaps.Clear();
        return true;
    }

    /// <summary>Emit only public quantities; no sampled pile or RNG state is serialized.</summary>
    public object Snapshot() => new
    {
        player = new
        {
            hp = new { current = State.PlayerHp, max = State.PlayerMaxHp }, block = State.PlayerBlock,
            energy = State.Energy, stars = State.Stars,
            powers = FormatPowers(State.PlayerBuffs, _playerPowerNames).Concat(_playerPassthrough).ToArray(),
            cards_played_this_turn = State.CardsPlayedThisTurn,
            attacks_played_this_turn = State.AttackCardsPlayedThisTurn,
            skills_played_this_turn = State.SkillCardsPlayedThisTurn,
        },
        enemies = State.Enemies.Select((enemy, i) => new
        {
            i = _enemies[i].Slot, enemy_id = _enemies[i].Id,
            hp = new { current = enemy.Hp, max = enemy.MaxHp }, block = enemy.Block,
            powers = FormatPowers(enemy.Buffs, _enemies[i].PowerNames).Concat(_enemies[i].Passthrough).ToArray(),
            alive = enemy.Hp > 0, hittable = _enemies[i].Hittable && enemy.Hp > 0,
        }).ToArray(),
        hand = State.Hand.Select((card, i) => new
        {
            handle = _hand[i].Handle, card_index = _hand[i].OriginalIndex, card_id = _hand[i].CardId,
            upgraded = card.Upgraded, cost = CombatEngine.EffectiveCost(card, State),
            target = Cards.Get(card.DefId).Target.ToString(),
            supported = _hand[i].InitialGap is null, boundary_reason = _hand[i].InitialGap,
        }).ToArray(),
        piles = new { draw = State.DrawPile.Count, discard = State.DiscardPile.Count, exhaust = State.ExhaustPile.Count },
        potions = PotionSnapshot(), pending_choice = ChoiceSnapshot(),
        stopped = Stopped, terminal = IsTerminal, projected_incoming_damage = ProjectedIncomingDamage,
        horizon = "current_player_turn_before_end_turn_effects",
        initial_preview_diagnostics = _previewDiagnostics,
        score_support = ScoreSupport,
    };

    /// <summary>Probe a private clone and stop at randomness, drawing, selection, or remapping.</summary>
    private string? CheckTransition(SimAction action, out CombatState? successor,
        bool allowReveals = false, Random? scenarioRandom = null)
    {
        successor = null;
        if (action.Type != "play_card") return "unsupported_action_type";
        int index = FindHand(action);
        if (index < 0) return "unknown_or_consumed_card_handle";
        var identity = _hand[index];
        if (identity.InitialGap is not null) return identity.InitialGap;
        if (_blockingGaps.Count > 0) return string.Join(";", _blockingGaps);
        var card = State.Hand[index];
        if (!CombatEngine.CanPlayFromHand(State, card)) return "core_card_not_playable";
        var def = Cards.Get(card.DefId);
        if (allowReveals && def.Name == "TrueGrit" && !card.Upgraded && State.Hand.Count > 1)
            return "random_hand_removal_identity_not_reviewed";
        bool opensChoice = State.Hand.Count > 1 && (def.Name == "BurningPact"
            || def.Name == "Armaments" && !card.Upgraded || def.Name == "TrueGrit" && card.Upgraded);
        if (allowReveals && opensChoice && (BuffSystem.Has(State.PlayerBuffs, BuffId.Corruption)
            || State.Relics.Any(relic => relic.DefId == RelicEffects.IronClub && relic.Counter == 3)
            || State.Relics.Any(relic => relic.DefId == RelicEffects.LetterOpener)
                && State.SkillCardsPlayedThisTurn % 3 == 2))
            return "selection_post_play_continuation_not_supported";
        if (def.Type == CardType.Attack && BuffSystem.Has(State.PlayerBuffs, BuffId.Juggling) && !_attackCountObserved)
            return "juggling_attack_count_not_observed";
        // Galvanic stamps individual Power cards; the core's board-wide shortcut lacks that stamp.
        if (def.Type == CardType.Power && State.Enemies.Any(enemy => enemy.Hp > 0 && BuffSystem.Has(enemy.Buffs, BuffId.Galvanic)))
            return "galvanic_card_affliction_not_imported";
        if (def.Type == CardType.Power && State.Relics.Any(relic => relic.DefId == RelicEffects.Permafrost))
            return "permafrost_first_power_status_not_observed";
        int enemyIndex = action.TargetIndex.HasValue ? _enemies.FindIndex(x => x.Slot == action.TargetIndex.Value) : -1;
        if (def.Target == CardTarget.AnyEnemy && (enemyIndex < 0 || State.Enemies[enemyIndex].Hp <= 0 || !_enemies[enemyIndex].Hittable))
            return "invalid_public_enemy_slot";
        if (def.Target != CardTarget.AnyEnemy && action.TargetIndex is not null) return "unexpected_target_for_card";
        if (def.Name == "Rampage" && identity.BonusDamageUnknown) return "rampage_per_copy_growth_not_observed";
        var clone = State.Clone();
        var initialEnemies = clone.Enemies.ToArray();
        try
        {
            CombatEngine.Step(clone, index, scenarioRandom ?? new BoundaryRandom(), enemyIndex);
            if (clone.PlayerHp <= 0 && _potions.Any(potion => potion.Occupied
                && Normalize(potion.Id) == "FAIRYINABOTTLE")) return "passive_potion_revival_not_reviewed";
            if (!allowReveals && clone.CardsDrawnThisCombat != State.CardsDrawnThisCombat) return "unknown_draw_boundary";
            if (!allowReveals && clone.PendingSelection is not null) return "card_selection_boundary";
            if (!allowReveals && clone.Hand.Count != State.Hand.Count - 1) return "hand_generation_or_auto_play_boundary";
            if (allowReveals && clone.Hand.Count > State.Hand.Count - 1 + clone.CardsDrawnThisCombat - State.CardsDrawnThisCombat)
                return "hand_generation_or_auto_play_boundary";
            if (allowReveals && clone.PendingSelection is { } selection)
            {
                if (!ReviewedChoices.Contains(selection.Kind)) return "selection_rule_not_reviewed";
                // The pinned core finishes post-play hooks before returning a choice.
                // These cases require a suspended source-card continuation instead.
                if (clone.CardsDrawnThisCombat != State.CardsDrawnThisCombat
                    || clone.AutoPlayQueue.Count > 0
                    || (selection.Kind is CardSelectionKind.ExhaustFromHandThenDraw or CardSelectionKind.ExhaustFromHand
                        && clone.DrawPile.Count < (selection.Kind == CardSelectionKind.ExhaustFromHandThenDraw ? selection.Amount : 0)
                            + BuffSystem.Get(clone.PlayerBuffs, BuffId.DarkEmbrace)))
                    return "selection_post_play_continuation_not_supported";
            }
            if (clone.Enemies.Any(enemy => !initialEnemies.Contains(enemy))) return "enemy_spawn_boundary";
            if (initialEnemies.Any(enemy => !clone.Enemies.Contains(enemy) && enemy.Hp > 0)) return "living_enemy_removal_boundary";
            // The core removes corpses. Restore those same object references for stable public slots.
            clone.Enemies = [.. initialEnemies];
            if (!clone.PlayerTurn || clone.EndTurnAfterPlay) return "automatic_end_turn_boundary";
            successor = clone;
            return null;
        }
        catch (RandomBoundaryException) { return "random_or_shuffle_boundary"; }
    }

    /// <summary>Resolve original input indices only; dynamic indices are never ambiguous.</summary>
    private int FindHand(SimAction action) => action.CardHandle is not null
        ? _hand.FindIndex(x => x.Handle == action.CardHandle)
        : _hand.FindIndex(x => x.OriginalIndex == action.CardIndex);

    /// <summary>Import each physical hand row separately even when card IDs repeat.</summary>
    private void ReadHand(JsonElement source)
    {
        string id = Text(source, "card_id");
        string line = Text(source, "line");
        int original = Number(source, "i", _hand.Count);
        string? gap = null;
        if (!CardIds.TryGetValue(Normalize(id), out var def))
        {
            gap = "unknown_card_id";
            def = CardIds["WOUND"];
        }
        else if (!ReviewedCards.Contains(def.Name)) gap = "card_rule_not_reviewed";
        bool upgraded = Boolean(source, "upgraded", Regex.IsMatch(line, @"^[^\[]+\+\d*\s*(\*\d+)?\s*\["));
        var card = new CardInstance(def.Id, upgraded);
        var modifiers = Items(Get(source, "mods")).Select(x => x.GetString() ?? "").ToArray();
        if (modifiers.Any(modifier => !EquivalentKeyword(modifier, card))) gap = "card_modifiers_not_imported";
        if (Get(source, "affliction").ValueKind == JsonValueKind.Object)
            gap = "card_affliction_not_reviewed:" + Text(Get(source, "affliction"), "affliction_id");
        if (Get(source, "enchantment").ValueKind == JsonValueKind.Object)
            gap = "card_enchantment_not_reviewed:" + Text(Get(source, "enchantment"), "enchantment_id");
        int visibleCost = Number(source, "energy_cost", def.Cost + (upgraded ? def.UpgradeCost : 0));
        int expectedCost = CombatEngine.EffectiveCost(card, State);
        if (!def.HasEnergyCostX && visibleCost != expectedCost)
        {
            // Public effective cost is a starting-point observation, not an inferred damage bonus.
            card = card with { CostBump = visibleCost - expectedCost };
            Support.Note($"{id}:visible_cost_override:{expectedCost}->{visibleCost}");
        }
        string unplayableReason = Text(source, "unplayable_reason", Text(source, "why"));
        bool initiallyPlayable = Boolean(source, "playable", true);
        if (!initiallyPlayable && !ResourceReason(unplayableReason) && CombatEngine.CanPlayFromHand(State, card))
            gap = "initial_public_unplayable_reason_not_modelled:" + unplayableReason;
        string? dynamicGap = ImportDynamicValues(source, def, ref card);
        if (dynamicGap is not null) gap = dynamicGap;
        bool hasPublicDamageBase = Items(Get(source, "dynamic_values")).Any(value =>
            Text(value, "name") == "Damage" && Get(value, "base_value").ValueKind == JsonValueKind.Number);
        bool rampageUnknown = def.Name == "Rampage" && !hasPublicDamageBase && NumberFromRules(line) is int shown
            && shown != BuffSystem.IncomingDamage(def.BaseDamage + (upgraded ? def.UpgradeDamage : 0), State.PlayerBuffs, []);
        CheckVisiblePreview($"h{_hand.Count}", id, line, card, def);
        State.Hand.Add(card);
        _hand.Add(new HandIdentity($"h{_hand.Count}", original, id, line, gap,
            initiallyPlayable, unplayableReason, rampageUnknown));
    }

    /// <summary>Use instance bases, never Power-adjusted previews, and reject unmapped overrides.</summary>
    private static string? ImportDynamicValues(JsonElement source, CardDef def, ref CardInstance card)
    {
        foreach (var value in Items(Get(source, "dynamic_values")))
        {
            string name = Text(value, "name");
            var baseValue = Get(value, "base_value");
            if (baseValue.ValueKind != JsonValueKind.Number || !baseValue.TryGetInt32(out int observedBase))
                return $"card_dynamic_base_not_observed:{name}";
            var enchanted = Get(value, "enchanted_value");
            if (enchanted.ValueKind == JsonValueKind.Number && enchanted.TryGetInt32(out int enchantedBase)
                && enchantedBase != observedBase)
                return $"card_dynamic_enchanted_value_not_imported:{name}:{observedBase}->{enchantedBase}";
            // Current values already include live modifiers or calculations; the core applies those itself.
            int? expected = ExpectedDynamicBase(def, card.Upgraded, name);
            if (expected is null) return $"card_dynamic_rule_not_reviewed:{name}";
            if (name == "Damage" && def.Name == "Rampage")
                card = card with { BonusDamage = observedBase - expected.Value };
            else if (observedBase != expected)
                return $"card_dynamic_base_not_imported:{name}:{expected}->{observedBase}";
        }
        return null;
    }

    /// <summary>Match public bases to the reviewed constants in fixed-core CardEffects, not its previews.</summary>
    private static int? ExpectedDynamicBase(CardDef def, bool upgraded, string name) => (def.Name, name) switch
    {
        (_, "Damage") => def.BaseDamage + (upgraded ? def.UpgradeDamage : 0),
        (_, "Block") => def.BaseBlock + (upgraded ? def.UpgradeBlock : 0),
        ("Uppercut", "Power") => upgraded ? 2 : 1,
        ("FightMe", "EnemyStrength") => 1,
        ("FightMe", "Repeat") => 2,
        ("Exterminate", "Repeat") => 4,
        ("FightMe", "StrengthPower" or "Strength") => upgraded ? 4 : 3,
        ("Dominate", "StrengthPerVulnerable") => 1,
        ("Dominate" or "Taunt", "VulnerablePower" or "Vulnerable") => upgraded ? 2 : 1,
        ("BodySlam", "CalculationBase" or "CalculatedDamage") => 0,
        ("BodySlam", "ExtraDamage") => 1,
        ("Vicious" or "PommelStrike", "Cards") => upgraded ? 2 : 1,
        ("Colossus", "Colossus") => 1,
        ("Hemokinesis" or "BloodWall", "HpLoss") => 2,
        ("DemonForm" or "Inflame" or "SetupStrike", "StrengthPower" or "Strength") => upgraded ? 3 : 2,
        ("DemonicShield", "CalculationBase" or "CalculatedBlock") => 0,
        ("DemonicShield", "HpLoss" or "CalculationExtra") => 1,
        ("BattleTrance", "Cards") => upgraded ? 4 : 3,
        ("Tremble", "VulnerablePower" or "Vulnerable") => upgraded ? 4 : 3,
        ("Bloodletting", "HpLoss") => 3,
        ("Bloodletting", "Energy") => upgraded ? 3 : 2,
        ("Pyre", "Energy") => upgraded ? 2 : 1,
        ("FeelNoPain", "Power") => upgraded ? 4 : 3,
        ("NotYet", "Heal") => upgraded ? 13 : 10,
        ("Rampage", "Increase") => upgraded ? 9 : 5,
        ("Bully", "CalculationBase" or "CalculatedDamage") => 4,
        ("Bully", "ExtraDamage") => upgraded ? 3 : 2,
        ("Cruelty", "CrueltyPower" or "Cruelty") => upgraded ? 50 : 25,
        ("FlameBarrier", "DamageBack") => upgraded ? 6 : 4,
        ("Conflagration", "Repeat") => upgraded ? 5 : 4,
        ("BurningPact", "Cards") => upgraded ? 3 : 2,
        ("Rage", "Power") => upgraded ? 5 : 3,
        ("Bash", "VulnerablePower" or "Vulnerable") => upgraded ? 3 : 2,
        ("Break", "VulnerablePower" or "Vulnerable") => upgraded ? 7 : 5,
        ("Offering", "HpLoss") => 6,
        ("Offering", "Energy") => 2,
        ("Offering", "Cards") => upgraded ? 5 : 3,
        ("Breakthrough", "HpLoss") => 1,
        ("ShrugItOff" or "Slimed", "Cards") => 1,
        ("Corruption", "Power") => 1,
        ("OneTwoPunch", "Attacks") => upgraded ? 2 : 1,
        ("Thunderclap", "VulnerablePower" or "Vulnerable") => 1,
        _ => null,
    };

    /// <summary>Compare visible text with a no-target preview without importing it as base damage.</summary>
    private void CheckVisiblePreview(string handle, string id, string line, CardInstance card, CardDef def)
    {
        int? visible = NumberFromRules(line);
        int? preview = null;
        if (def.BaseDamage > 0)
        {
            int baseDamage = def.BaseDamage + (card.Upgraded ? def.UpgradeDamage : 0)
                + RelicEffects.CardDamageBonus(State, def, card.Upgraded);
            preview = BuffSystem.IncomingDamage(baseDamage, State.PlayerBuffs, [],
                cardMultiplier: RelicEffects.CardDamageMultiplier(State, def));
        }
        else if (def.BaseBlock > 0)
        {
            int baseBlock = def.BaseBlock + (card.Upgraded ? def.UpgradeBlock : 0);
            preview = BuffSystem.IncomingBlock(baseBlock, State.PlayerBuffs, def.DefendTag);
        }
        if (visible is null || preview is null) return;
        _previewDiagnostics.Add(new
        {
            card_handle = handle, card_id = id, first_visible_rule_number = visible,
            core_preview_without_target = preview, matches = visible == preview,
            note = "diagnostic_only; target context and calculated variables may differ; visible number is never used as base damage",
        });
    }

    /// <summary>Expand public grouped counts; sorted representatives never become draw knowledge.</summary>
    private void ReadPile(JsonElement source, List<CardInstance> pile, string name)
    {
        foreach (var group in Items(source))
        {
            var ids = Items(Get(group, "card_ids")).Select(x => x.GetString() ?? "").ToArray();
            string line = Text(group, "line");
            int count = 1;
            var match = Regex.Match(line, @"^[^\[]*?\*(\d+)(?=\s|\[|$)");
            if (match.Success) count = int.Parse(match.Groups[1].Value);
            if (ids.Length == 0) { Support.Note($"{name}:missing_card_ids"); continue; }
            if (ids.Length > 1) Support.Note($"{name}:grouped_card_identity_ambiguous");
            if (!CardIds.TryGetValue(Normalize(ids[0]), out var def))
            {
                Support.Note($"{name}:unknown_card_id:{ids[0]}", ids[0]);
                def = CardIds["WOUND"];
            }
            bool upgraded = Regex.IsMatch(line, @"\+\d*\s*(\*\d+)?\s*\[");
            for (int i = 0; i < count; i++) pile.Add(new CardInstance(def.Id, upgraded));
        }
    }

    /// <summary>Map public powers while retaining original names for replay comparison.</summary>
    private void ReadPowers(JsonElement source, List<BuffState> buffs, Dictionary<BuffId, string> names,
        List<string> passthrough, string owner)
    {
        foreach (var power in Items(source))
        {
            string value = power.ValueKind == JsonValueKind.String ? power.GetString()! : "";
            var match = Regex.Match(value, @"^(\S+)\s+(-?\d+)");
            if (!match.Success) { Support.Note($"{owner}:unparsed_power:{value}"); _blockingGaps.Add($"{owner}:unparsed_power"); continue; }
            string id = match.Groups[1].Value;
            string normalized = Normalize(id).Replace("POWER", "", StringComparison.Ordinal);
            int amount = int.Parse(match.Groups[2].Value);
            if (normalized == "FLEXPOTION") { passthrough.Add(value); continue; }
            // The live Crimson Mantle counter does not expose the core's separate HP/block values.
            // Its only trigger is next turn; keep it visible, but never invent those future values.
            if (normalized == "CRIMSONMANTLE") { passthrough.Add(value); continue; }
            if (normalized == "SETUPSTRIKE") normalized = "TEMPORARYSTRENGTH";
            if (normalized == "SHACKLINGPOTION") normalized = "TEMPORARYSTRENGTH";
            if (!PowerIds.TryGetValue(normalized, out var buff))
            {
                Support.Note($"{owner}:unknown_power:{id}", id);
                _blockingGaps.Add($"{owner}:unknown_power:{id}");
                passthrough.Add(value);
                continue;
            }
            buffs.Add(new BuffState(buff, amount));
            names[buff] = id;
            if (!ReviewedPowers.Contains(buff.ToString()))
            {
                Support.Note($"{owner}:power_rule_not_reviewed:{id}");
                _blockingGaps.Add($"{owner}:power_rule_not_reviewed:{id}");
            }
        }
    }

    /// <summary>Import existing relic effects without firing their combat-start hooks again.</summary>
    private void ReadRelics(JsonElement run, JsonElement player)
    {
        var stacks = Items(Get(run, "relic_stacks")).ToArray();
        int index = 0;
        foreach (var item in Items(Get(run, "relic_ids")))
        {
            string id = item.GetString() ?? "";
            if (!RelicIds.TryGetValue(Normalize(id), out var relic))
            {
                Support.Note($"unknown_relic:{id}", id); _blockingGaps.Add($"unknown_relic:{id}"); index++; continue;
            }
            bool hasCounter = index < stacks.Length && stacks[index].ValueKind == JsonValueKind.Number
                && stacks[index].TryGetInt32(out _);
            int counter = hasCounter ? stacks[index].GetInt32() : 0;
            if (relic.Name is "OrnamentalFan" or "Shuriken" or "Kunai") counter = State.AttackCardsPlayedThisTurn % 3;
            if (relic.Name == "LetterOpener") counter = State.SkillCardsPlayedThisTurn % 3;
            State.Relics.Add(new RelicInstance(relic.Id, counter));
            if (!ReviewedRelics.Contains(relic.Name))
            {
                Support.Note($"relic_rule_not_reviewed:{id}"); _blockingGaps.Add($"relic_rule_not_reviewed:{id}");
            }
            // Akabeko only grants Vigor at combat start; the current public Vigor is sufficient.
            // Reapplying the opening hook here would duplicate an already observed bonus.
            if (relic.Name == "IronClub" && (!hasCounter || counter is < 0 or > 3))
            {
                Support.Note("iron_club_run_counter_not_observed");
                _blockingGaps.Add("iron_club_run_counter_not_observed");
            }
            if (relic.Name == "BrilliantScarf"
                && (Get(player, "cards_played_this_turn").ValueKind != JsonValueKind.Number
                    || !Get(player, "cards_played_this_turn").TryGetInt32(out _)))
            {
                Support.Note("brilliant_scarf_cards_played_counter_not_observed");
                _blockingGaps.Add("brilliant_scarf_cards_played_counter_not_observed");
            }
            index++;
        }
    }

    /// <summary>Retain public enemy slots and current intent; never import a hidden move ID.</summary>
    private void ReadEnemy(JsonElement source)
    {
        string id = Text(source, "enemy_id");
        var hp = ReadHp(Get(source, "hp"));
        var enemy = new EnemyState { Hp = hp.Current, MaxHp = hp.Max, Block = Number(source, "block") };
        if (!EnemyIds.TryGetValue(Normalize(id), out enemy.DefId))
        {
            Support.Note($"unknown_enemy:{id}", id); _blockingGaps.Add($"unknown_enemy:{id}");
        }
        var identity = new EnemyIdentity(Number(source, "i", _enemies.Count), id, Boolean(source, "hittable", true), [], [], []);
        ReadPowers(Get(source, "powers"), enemy.Buffs, identity.PowerNames, identity.Passthrough, id);
        var intents = Items(Get(source, "intents")).ToArray();
        identity = identity with
        {
            HasNonAttackIntent = intents.Any(intent => !Text(intent, "intent_type").Contains("Attack", StringComparison.OrdinalIgnoreCase)),
        };
        var attacks = intents.Where(x => Number(x, "damage") > 0 || Number(x, "total_damage") > 0)
            .Select(attack => ReadAttackIntent(attack, enemy, id)).ToArray();
        enemy.CurrentIntent = attacks.Length > 0 ? attacks[0] : new Intent(IntentType.Unknown, 0);
        identity.AdditionalAttacks.AddRange(attacks.Skip(1));
        State.Enemies.Add(enemy);
        _enemies.Add(identity);
    }

    /// <summary>Recover each visible attack separately so later debuffs affect every hit group.</summary>
    private Intent ReadAttackIntent(JsonElement attack, EnemyState enemy, string id)
    {
        int damage = Number(attack, "damage");
        int hits = Math.Max(1, Number(attack, "hits", 1));
        if (damage == 0)
        {
            int total = Number(attack, "total_damage");
            damage = total / hits;
            if (total % hits != 0)
                Support.Note($"{id}:intent_damage_total_not_divisible_by_hits:incoming_projection_is_estimate");
        }
        int baseDamage = damage;
        var possibleBases = new List<int>();
        for (int candidate = 0; candidate <= damage * 4 + 100; candidate++)
        {
            if (BuffSystem.IncomingDamage(candidate, enemy.Buffs, State.PlayerBuffs,
                weakDelta: RelicEffects.WeakMultiplierDeltaAgainstPlayer(State)) == damage)
                possibleBases.Add(candidate);
        }
        if (possibleBases.Count > 0) baseDamage = possibleBases[^1];
        if (possibleBases.Count > 1) Support.Note($"{id}:intent_damage_rounding_ambiguous:{possibleBases[0]}..{possibleBases[^1]}:incoming_projection_is_conservative_estimate");
        if (possibleBases.Count == 0) Support.Note($"{id}:intent_damage_could_not_be_inverted:incoming_projection_is_estimate");
        return new Intent(IntentType.Attack, baseDamage, hits);
    }

    /// <summary>Serialize canonical public power IDs and debuff flags, excluding internal counters.</summary>
    private static IEnumerable<string> FormatPowers(List<BuffState> buffs, Dictionary<BuffId, string> names)
    {
        foreach (var buff in buffs)
        {
            if (buff.Magnitude == 0) continue;
            // Stunned is an emulator AI flag. The native public model has no such Power entity.
            if (buff.Id == BuffId.Stunned) continue;
            string id = names.TryGetValue(buff.Id, out var original) ? original
                : buff.Id == BuffId.TemporaryStrength ? "SETUP_STRIKE_POWER" : ToSlug(buff.Id.ToString());
            bool debuff = id == "SHACKLING_POTION_POWER" ? true
                : BuffSystem.AllowsNegative(buff.Id) ? buff.Magnitude < 0 || buff.Id == BuffId.Shrink
                : PowerBuffTypes.TryGetValue(Normalize(id), out bool isBuff) ? !isBuff
                : BuffSystem.IsDebuffApplication(buff.Id, buff.Magnitude);
            yield return $"{id} {buff.Magnitude}" + (debuff ? " [debuff]" : "");
        }
    }

    /// <summary>Allow tags already represented by the core definition, never unknown enchantments.</summary>
    private static bool EquivalentKeyword(string modifier, CardInstance card) => Normalize(modifier) switch
    {
        "EXHAUST" => card.IsExhaust(),
        "ETHEREAL" => card.IsEthereal(),
        "RETAIN" => card.IsRetained(),
        "INNATE" => card.IsInnate(),
        "UNPLAYABLE" => Cards.Get(card.DefId).Unplayable,
        _ => false,
    };

    /// <summary>Resource shortages can become legal after another simulated card resolves.</summary>
    private static bool ResourceReason(string reason) => reason.Contains("energy", StringComparison.OrdinalIgnoreCase)
        || reason.Contains("stars", StringComparison.OrdinalIgnoreCase)
        || reason.Contains("resource", StringComparison.OrdinalIgnoreCase)
        || reason.Contains("能量", StringComparison.Ordinal) || reason.Contains("星星", StringComparison.Ordinal);

    /// <summary>Extract the first visible damage number solely for mismatch diagnostics.</summary>
    private static int? NumberFromRules(string line)
    {
        string rules = Regex.Replace(line, @"^.*?\]", "");
        var match = Regex.Match(rules, @"\d+");
        return match.Success ? int.Parse(match.Value) : null;
    }

    /// <summary>Normalize public slugs and upstream PascalCase without depending on locale.</summary>
    private static string Normalize(string value) => new(value.Where(char.IsLetterOrDigit).Select(char.ToUpperInvariant).ToArray());

    /// <summary>Build a public power slug for effects created inside a simulated branch.</summary>
    private static string ToSlug(string value)
    {
        string slug = Regex.Replace(value, "([a-z0-9])([A-Z])", "$1_$2").ToUpperInvariant();
        return slug.EndsWith("_POWER", StringComparison.Ordinal) ? slug : slug + "_POWER";
    }

    /// <summary>Produce a cache key for one fixed position's physical card and target.</summary>
    private static string ActionKey(SimAction action) => $"{action.CardHandle}:{action.CardIndex}:{action.TargetIndex}";

    /// <summary>Read an optional property without conflating absence with zero.</summary>
    private static JsonElement Get(JsonElement source, string key) => source.ValueKind == JsonValueKind.Object
        && source.TryGetProperty(key, out var value) ? value : default;

    /// <summary>Enumerate public arrays, treating absent optional sections as empty.</summary>
    private static IEnumerable<JsonElement> Items(JsonElement source) => source.ValueKind == JsonValueKind.Array
        ? source.EnumerateArray() : [];

    /// <summary>Read a public integer with an explicit caller-provided default.</summary>
    private static int Number(JsonElement source, string key, int fallback = 0)
    {
        var value = Get(source, key);
        return value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int number) ? number : fallback;
    }

    /// <summary>Read a public string while preserving dynamic text as diagnostics.</summary>
    private static string Text(JsonElement source, string key, string fallback = "") => Get(source, key) is var value
        && value.ValueKind == JsonValueKind.String ? value.GetString()! : fallback;

    /// <summary>Read an optional flag without treating null as false.</summary>
    private static bool Boolean(JsonElement source, string key, bool fallback = false) => Get(source, key).ValueKind switch
    { JsonValueKind.True => true, JsonValueKind.False => false, _ => fallback };

    /// <summary>Accept public current/max strings and normalized test HP objects.</summary>
    private static (int Current, int Max) ReadHp(JsonElement source)
    {
        if (source.ValueKind == JsonValueKind.Object) return (Number(source, "current"), Number(source, "max"));
        if (source.ValueKind == JsonValueKind.Number) return (source.GetInt32(), source.GetInt32());
        var values = Regex.Matches(source.ValueKind == JsonValueKind.String ? source.GetString()! : "", @"-?\d+");
        return values.Count > 0 ? (int.Parse(values[0].Value), int.Parse(values[Math.Min(1, values.Count - 1)].Value)) : (0, 0);
    }

    /// <summary>Associate one current-hand row with its stable input identity.</summary>
    private sealed record HandIdentity(string Handle, int OriginalIndex, string CardId, string Line,
        string? InitialGap, bool InitiallyPlayable, string? UnplayableReason, bool BonusDamageUnknown);

    /// <summary>Keep enemy slot metadata out of the upstream mutable rule objects.</summary>
    private sealed record EnemyIdentity(int Slot, string Id, bool Hittable,
        Dictionary<BuffId, string> PowerNames, List<string> Passthrough, List<Intent> AdditionalAttacks,
        bool HasNonAttackIntent = false)
    {
        /// <summary>Copy name maps for an independent search branch.</summary>
        public EnemyIdentity Copy() => new(Slot, Id, Hittable, new(PowerNames), [.. Passthrough],
            [.. AdditionalAttacks], HasNonAttackIntent);
    }

    /// <summary>Abort private probes at any attempted RNG use, before results are exposed.</summary>
    private sealed class RandomBoundaryException : Exception;

    /// <summary>No actual game seed or experimental random result enters deterministic planning.</summary>
    private sealed class BoundaryRandom : Random
    {
        /// <summary>Refuse each random API used by upstream rules.</summary>
        public override int Next() => throw new RandomBoundaryException();
        /// <summary>Refuse each random API used by upstream rules.</summary>
        public override int Next(int maxValue) => throw new RandomBoundaryException();
        /// <summary>Refuse each random API used by upstream rules.</summary>
        public override int Next(int minValue, int maxValue) => throw new RandomBoundaryException();
        /// <summary>Refuse each random API used by upstream rules.</summary>
        public override double NextDouble() => throw new RandomBoundaryException();
        /// <summary>Refuse long integer sampling from future upstream rules.</summary>
        public override long NextInt64() => throw new RandomBoundaryException();
        /// <summary>Refuse long integer sampling from future upstream rules.</summary>
        public override long NextInt64(long maxValue) => throw new RandomBoundaryException();
        /// <summary>Refuse long integer sampling from future upstream rules.</summary>
        public override long NextInt64(long minValue, long maxValue) => throw new RandomBoundaryException();
        /// <summary>Refuse single-precision sampling from future upstream rules.</summary>
        public override float NextSingle() => throw new RandomBoundaryException();
        /// <summary>Refuse fallback sampling paths used by base Random methods.</summary>
        protected override double Sample() => throw new RandomBoundaryException();
        /// <summary>Refuse byte sampling from any future upstream rule.</summary>
        public override void NextBytes(byte[] buffer) => throw new RandomBoundaryException();
        /// <summary>Refuse byte sampling from any future upstream rule.</summary>
        public override void NextBytes(Span<byte> buffer) => throw new RandomBoundaryException();
    }
}
