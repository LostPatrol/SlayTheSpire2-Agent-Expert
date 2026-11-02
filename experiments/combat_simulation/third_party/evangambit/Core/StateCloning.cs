// Copies combat data without retaining per-play creature snapshot references.
namespace Sts2Emulator.Core;

using Sts2Emulator.Core.Rng;
using Sts2Emulator.Core.Run;

/// <summary>
/// Deep copies of the mutable simulation state, so a search can fork a position
/// instead of replaying the run to reach it.
/// </summary>
/// <remarks>
/// <para>
/// Everything here is hand-written rather than reflected, because the core is
/// NativeAOT. That makes it drift-prone -- a field added to a state class and not
/// added here is a clone that silently shares it -- so <c>StateCloningTests</c>
/// walks every field by reflection and fails when one is missed.
/// </para>
/// <para>
/// A plain clone is a faithful copy, which for a tree search is an oracle: every
/// run-level stream derives from the run seed, so rolling forward reads the exact
/// rewards, shop stock and encounter compositions still to come. Resampling is what
/// makes a clone safe to search with -- see <c>RunEngine.ResampleHiddenState</c> and
/// docs/agent-interface.md.
/// </para>
/// </remarks>
public static class StateCloning
{
    public static GameRng Clone(this GameRng rng)
    {
        var copy = new GameRng(rng.RawSeed);
        copy.AdvanceToCallCount(rng.CallCount);
        return copy;
    }

    /// <summary>
    /// A copy of this stream, from a DIFFERENT seed but at the same call count.
    /// </summary>
    /// <remarks>
    /// The position is kept deliberately. A stream's call count is not hidden information --
    /// it is a fact about how much has happened -- so a determinization should change what
    /// the next value IS, not how far along the sequence stands. Keeping it also means a
    /// reseeded copy stays comparable to the original in the stream-position diff.
    /// </remarks>
    public static CountingRandom? Reseeded(this CountingRandom? rng, int? seed)
    {
        if (rng is null)
        {
            return null;
        }

        if (seed is null)
        {
            return rng.Clone();
        }

        var copy = new CountingRandom(seed.Value);
        for (int i = 0; i < rng.CallCount; i++)
        {
            copy.Next();
        }

        return copy;
    }

    public static CountingRandom Clone(this CountingRandom rng)
    {
        var copy = new CountingRandom(rng.Seed);
        for (int i = 0; i < rng.CallCount; i++)
        {
            copy.Next();
        }

        return copy;
    }

    public static EnemyState Clone(this EnemyState enemy) =>
        new()
        {
            DefId = enemy.DefId,
            Hp = enemy.Hp,
            MaxHp = enemy.MaxHp,
            Block = enemy.Block,
            CurrentIntent = enemy.CurrentIntent,
            SecondaryIntent = enemy.SecondaryIntent,
            Buffs = [.. enemy.Buffs],
            LastSpawnedDefId = enemy.LastSpawnedDefId,
            PoweredHitsThisTurn = enemy.PoweredHitsThisTurn,
            MoveIndex = enemy.MoveIndex,
            LastMove = enemy.LastMove,
            LastMoveRepeats = enemy.LastMoveRepeats,
            OnceOnlyMoveUsed = enemy.OnceOnlyMoveUsed,
            MoveHistory = [.. enemy.MoveHistory],
            StartsOnBranch = enemy.StartsOnBranch,
            RollsNextMove = enemy.RollsNextMove,
            NemesisIntangibleOn = enemy.NemesisIntangibleOn,
            StarterMove = enemy.StarterMove,
            Slot = enemy.Slot,
            StolenGold = enemy.StolenGold,
            HeistGold = enemy.HeistGold,
            StolenCard = enemy.StolenCard,
            Escaped = enemy.Escaped,
            OffBalance = enemy.OffBalance,
            LastBranch = enemy.LastBranch,
            RepeatStreak = enemy.RepeatStreak,
            BranchCooldown = enemy.BranchCooldown,
        };

    /// <summary>Preserve the pending decision and its follow-up without sharing mutable lists.</summary>
    public static PendingCardSelection Clone(this PendingCardSelection selection) =>
        new()
        {
            Kind = selection.Kind,
            Candidates = [.. selection.Candidates],
            SourceCardDefId = selection.SourceCardDefId,
            Amount = selection.Amount,
            GeneratedCandidates = [.. selection.GeneratedCandidates],
            AfterSelectionToHand = [.. selection.AfterSelectionToHand],
            Skippable = selection.Skippable,
            GeneratedUpgraded = selection.GeneratedUpgraded,
            GeneratedRetained = selection.GeneratedRetained,
        };

    /// <summary>
    /// Copy a combat, optionally RESEEDING every random stream it owns.
    /// </summary>
    /// <remarks>
    /// Without a seed this is a faithful copy: same streams, same seeds, same call counts,
    /// so the copy plays out exactly as the original would. That is what the differential
    /// tests want and must not change.
    ///
    /// With one, each stream is rebuilt from the new seed and fast-forwarded to the SAME
    /// call count -- different values, identical positions. That is the latent half of a
    /// determinization: every roll still to come (enemy moves, card generation, targeting,
    /// orb and HP rolls) resolves differently in each copy, which is what a search over an
    /// imperfect-information game needs and a plain clone cannot give it. `RunState.Clone`
    /// has taken a `stringSeed` for this reason; combat had no equivalent.
    ///
    /// It does NOT touch the draw pile. That order was materialised by a shuffle that has
    /// already happened, so reseeding cannot undo it -- see `ReshuffleUnknownDrawOrder`.
    /// </remarks>
    public static CombatState Clone(this CombatState combat, int? rngSeed = null) =>
        new()
        {
            AscensionLevel = combat.AscensionLevel,
            PlayerHp = combat.PlayerHp,
            PlayerMaxHp = combat.PlayerMaxHp,
            PlayerBlock = combat.PlayerBlock,
            Energy = combat.Energy,
            MaxEnergy = combat.MaxEnergy,
            PlayerGold = combat.PlayerGold,
            StolenBackGold = combat.StolenBackGold,
            RoyaltiesGold = combat.RoyaltiesGold,
            FatGremlinEscaped = combat.FatGremlinEscaped,
            BattlewornDummyRanOutOfTime = combat.BattlewornDummyRanOutOfTime,
            PaelsLegionCooldown = combat.PaelsLegionCooldown,
            MercGoldWasStolen = combat.MercGoldWasStolen,
            Hand = [.. combat.Hand],
            DrawPile = [.. combat.DrawPile],
            DiscardPile = [.. combat.DiscardPile],
            ExhaustPile = [.. combat.ExhaustPile],
            ReturnToHandBeforeDraw = [.. combat.ReturnToHandBeforeDraw],
            CopiesToHandBeforeDraw = [.. combat.CopiesToHandBeforeDraw],
            EndTurnAwaitingSelection = combat.EndTurnAwaitingSelection,
            AfterimageBeforePlay = combat.AfterimageBeforePlay,
            StormBeforePlay = combat.StormBeforePlay,
            SubroutineBeforePlay = combat.SubroutineBeforePlay,
            StatusCardsDrawnThisTurn = combat.StatusCardsDrawnThisTurn,
            ResolvingDefendCard = combat.ResolvingDefendCard,
            PlayedCardCostForCombat = combat.PlayedCardCostForCombat,
            AutoPlayQueue = [.. combat.AutoPlayQueue],
            Orbs = [.. combat.Orbs],
            OrbCapacity = combat.OrbCapacity,
            BaseOrbSlots = combat.BaseOrbSlots,
            OstyHp = combat.OstyHp,
            OstyMaxHp = combat.OstyMaxHp,
            Stars = combat.Stars,
            OrbitEnergySpent = combat.OrbitEnergySpent,
            PotionSlots = (int[])combat.PotionSlots.Clone(),
            MaxPotionSlots = combat.MaxPotionSlots,
            Relics = [.. combat.Relics],
            PlayerBuffs = [.. combat.PlayerBuffs],
            PlayerDebuffsAtRoundStart = [.. combat.PlayerDebuffsAtRoundStart],
            Enemies = [.. combat.Enemies.Select(enemy => enemy.Clone())],
            EncounterId = combat.EncounterId,
            IsEliteCombat = combat.IsEliteCombat,
            ShuffleRng = combat.ShuffleRng.Reseeded(rngSeed),
            TargetRng = combat.TargetRng.Reseeded(rngSeed),
            CardSelectionRng = combat.CardSelectionRng.Reseeded(rngSeed),
            EnergyCostRng = combat.EnergyCostRng?.Clone(),
            CardGenerationRng = combat.CardGenerationRng.Reseeded(rngSeed),
            PotionGenerationRng = combat.PotionGenerationRng.Reseeded(rngSeed),
            OrbGenerationRng = combat.OrbGenerationRng.Reseeded(rngSeed),
            // A CountingRandom carries its seed and position, so it clones exactly. A
            // plain Random does not, and only the combat-only environment supplies one
            // -- that path has no clone export, so the shared reference is unreachable.
            AiRng = combat.AiRng is CountingRandom counting
                ? counting.Reseeded(rngSeed)
                : combat.AiRng,
            NicheHpRng = combat.NicheHpRng.Reseeded(rngSeed),
            PendingSelection = combat.PendingSelection?.Clone(),
            AutoPlaying = combat.AutoPlaying,
            PlayedCardBonusDamage = combat.PlayedCardBonusDamage,
            PlayedCardBonusBlock = combat.PlayedCardBonusBlock,
            EnergySpentThisTurn = combat.EnergySpentThisTurn,
            PenNibArmed = combat.PenNibArmed,
            EtherealExhaustsThisTurn = combat.EtherealExhaustsThisTurn,
            VambraceSpent = combat.VambraceSpent,
            UnblockedDamageThisTurn = combat.UnblockedDamageThisTurn,
            RainbowRingAttacks = combat.RainbowRingAttacks,
            RainbowRingSkills = combat.RainbowRingSkills,
            RainbowRingPowers = combat.RainbowRingPowers,
            RainbowRingPaidThisTurn = combat.RainbowRingPaidThisTurn,
            UnsettlingLampSpent = combat.UnsettlingLampSpent,
            UnsettlingLampCard = combat.UnsettlingLampCard,
            GamblingChipDiscarded = combat.GamblingChipDiscarded,
            GamblingChipOwed = combat.GamblingChipOwed,
            BurningSticksUsed = combat.BurningSticksUsed,
            BeltBuckleApplied = combat.BeltBuckleApplied,
            IsEliteRoom = combat.IsEliteRoom,
            TookUnblockedDamage = combat.TookUnblockedDamage,
            PlayedCardEnchantSpent = combat.PlayedCardEnchantSpent,
            PlayedCardEnchantGrew = combat.PlayedCardEnchantGrew,
            PlayedCardCostBump = combat.PlayedCardCostBump,
            ToricToughnessBlock = combat.ToricToughnessBlock,
            Turn = combat.Turn,
            PlayerTurn = combat.PlayerTurn,
            LastAttackOrSkillThisTurn = combat.LastAttackOrSkillThisTurn,
            LastAttackOrSkillLastTurn = combat.LastAttackOrSkillLastTurn,
            SkillPlayedWhileSmoggy = combat.SkillPlayedWhileSmoggy,
            CardsPlayedThisTurn = combat.CardsPlayedThisTurn,
            AttackCardsPlayedThisTurn = combat.AttackCardsPlayedThisTurn,
            AttackOrSkillCardsPlayedThisTurn = combat.AttackOrSkillCardsPlayedThisTurn,
            CardPlaysThisTurn = combat.CardPlaysThisTurn,
            CardsPlayedThisCombat = combat.CardsPlayedThisCombat,
            DrawnCardsSinceAutomationProc = combat.DrawnCardsSinceAutomationProc,
            CardsDrawnThisCombat = combat.CardsDrawnThisCombat,
            ShivsPlayedThisTurn = combat.ShivsPlayedThisTurn,
            ExtraCardRewards = combat.ExtraCardRewards,
            StolenBackCards = [.. combat.StolenBackCards],
            ExecutedThisEnemyTurn = [.. combat.ExecutedThisEnemyTurn],
            AutoPlayTargetIndex = combat.AutoPlayTargetIndex,
            CardsPlayedSincePanacheProc = combat.CardsPlayedSincePanacheProc,
            BlockGainsThisTurn = combat.BlockGainsThisTurn,
            PlayerHpLostThisTurn = combat.PlayerHpLostThisTurn,
            CardsExhaustedThisTurn = combat.CardsExhaustedThisTurn,
            LightningOrbsChanneledThisCombat = combat.LightningOrbsChanneledThisCombat,
            EtherealExhaustCount = combat.EtherealExhaustCount,
            EtherealCardPlaysThisCombat = combat.EtherealCardPlaysThisCombat,
            LethalAttackPlay = combat.LethalAttackPlay,
            SkillCardsPlayedThisTurn = combat.SkillCardsPlayedThisTurn,
            BoundCardsThisTurn = combat.BoundCardsThisTurn,
            BoundCardPlayedThisTurn = combat.BoundCardPlayedThisTurn,
            MusicBoxUsedThisTurn = combat.MusicBoxUsedThisTurn,
            FurCoatBites = combat.FurCoatBites,
            WhisperingEarringSpent = combat.WhisperingEarringSpent,
            CardPlaysLastTurn = combat.CardPlaysLastTurn,
            ExtraCardsDrawnThisTurn = combat.ExtraCardsDrawnThisTurn,
            DoomAppliedThisTurn = combat.DoomAppliedThisTurn,
            OstyAttacksThisTurn = combat.OstyAttacksThisTurn,
            StarsGainedThisTurn = combat.StarsGainedThisTurn,
            PlayedCardStarsSpent = combat.PlayedCardStarsSpent,
            VoidFormCardsPlayedThisTurn = combat.VoidFormCardsPlayedThisTurn,
            EndTurnAfterPlay = combat.EndTurnAfterPlay,
            CardsGeneratedThisCombat = combat.CardsGeneratedThisCombat,
            TenderCardsPlayedThisTurn = combat.TenderCardsPlayedThisTurn,
            UnblockedDamageHitCount = combat.UnblockedDamageHitCount,
            TargetEnemyIndex = combat.TargetEnemyIndex,
            KnownTopCount = combat.KnownTopCount,
            KnownBottomCount = combat.KnownBottomCount,
        };

    public static RunMapNode Clone(this RunMapNode node)
    {
        var copy = new RunMapNode(node.Col, node.Row)
        {
            NodeType = node.NodeType,
            EncounterId = node.EncounterId,
            CanBeModified = node.CanBeModified,
        };
        copy.Children.AddRange(node.Children);
        copy.Parents.AddRange(node.Parents);
        return copy;
    }

    /// <summary>
    /// A RunRngSet is twelve streams off one seed, so it rebuilds from the seed and
    /// then fast-forwards each stream to where the original had got to.
    /// </summary>
    public static RunRngSet CloneAt(string stringSeed, RunRngSet source)
    {
        var copy = new RunRngSet(stringSeed);
        copy.UpFront.AdvanceToCallCount(source.UpFront.CallCount);
        copy.Shuffle.AdvanceToCallCount(source.Shuffle.CallCount);
        copy.UnknownMapPoint.AdvanceToCallCount(source.UnknownMapPoint.CallCount);
        copy.CombatCardGeneration.AdvanceToCallCount(source.CombatCardGeneration.CallCount);
        copy.CombatPotionGeneration.AdvanceToCallCount(source.CombatPotionGeneration.CallCount);
        copy.CombatCardSelection.AdvanceToCallCount(source.CombatCardSelection.CallCount);
        copy.CombatEnergyCosts.AdvanceToCallCount(source.CombatEnergyCosts.CallCount);
        copy.CombatTargets.AdvanceToCallCount(source.CombatTargets.CallCount);
        copy.MonsterAi.AdvanceToCallCount(source.MonsterAi.CallCount);
        copy.Niche.AdvanceToCallCount(source.Niche.CallCount);
        copy.CombatOrbs.AdvanceToCallCount(source.CombatOrbs.CallCount);
        copy.TreasureRoomRelics.AdvanceToCallCount(source.TreasureRoomRelics.CallCount);
        return copy;
    }

    public static PlayerRngSet CloneAt(RunRngSet runRng, PlayerRngSet source)
    {
        var copy = new PlayerRngSet(runRng);
        copy.Rewards.AdvanceToCallCount(source.Rewards.CallCount);
        copy.Shops.AdvanceToCallCount(source.Shops.CallCount);
        copy.Transformations.AdvanceToCallCount(source.Transformations.CallCount);
        return copy;
    }

    /// <summary>
    /// A deep copy of the run. Pass a different <paramref name="stringSeed" /> to
    /// rebuild every stream off a new seed, which resamples everything the run has
    /// not yet paid out -- rewards, shop stock, encounter composition, shuffles --
    /// while leaving what has already happened exactly as it was.
    /// </summary>
    public static RunState Clone(this RunState state, string? stringSeed = null)
    {
        var rng = CloneAt(stringSeed ?? state.StringSeed, state.Rng);
        var mapNodes = new Dictionary<(int Col, int Row), RunMapNode>(state.MapNodes.Count);
        foreach (var (coord, node) in state.MapNodes)
        {
            mapNodes[coord] = node.Clone();
        }

        return new RunState
        {
            StringSeed = state.StringSeed,
            Rng = rng,
            PlayerRng = CloneAt(rng, state.PlayerRng),
            PlayerHp = state.PlayerHp,
            PlayerMaxHp = state.PlayerMaxHp,
            Gold = state.Gold,
            Floor = state.Floor,
            Phase = state.Phase,
            Deck = [.. state.Deck],
            Relics = [.. state.Relics],
            UsedUpRelics = [.. state.UsedUpRelics],
            MaxPotionSlots = state.MaxPotionSlots,
            CardRewardRerollAvailable = state.CardRewardRerollAvailable,
            LastingCandyRewardCard = state.LastingCandyRewardCard,
            LastingCandyRewardUpgraded = state.LastingCandyRewardUpgraded,
            ExtraCardRewardsOwed = state.ExtraCardRewardsOwed,
            CardRemovalsOwed = state.CardRemovalsOwed,
            RoyaltiesGoldOwed = state.RoyaltiesGoldOwed,
            RestOptionsTaken = state.RestOptionsTaken,
            TookUnblockedDamageThisCombat = state.TookUnblockedDamageThisCombat,
            RewardEnchantIndex = state.RewardEnchantIndex,
            RewardEnchantment = state.RewardEnchantment,
            PendingBonusRelicRewards = [.. state.PendingBonusRelicRewards],
            WaxRelics = [.. state.WaxRelics],
            FurCoatRooms = [.. state.FurCoatRooms],
            FurCoatAct = state.FurCoatAct,
            GoldenPathAct = state.GoldenPathAct,
            ParasolSweepPending = state.ParasolSweepPending,
            LavaRockTriggered = state.LavaRockTriggered,
            PendingSpecialCardReward = state.PendingSpecialCardReward,
            ResumeEventId = state.ResumeEventId,
            ResumeEventPage = state.ResumeEventPage,
            ResumeEventDummyEscaped = state.ResumeEventDummyEscaped,
            PendingNeowsBonesCurse = state.PendingNeowsBonesCurse,
            PendingHeftyTabletCurse = state.PendingHeftyTabletCurse,
            PendingPotionRewards = [.. state.PendingPotionRewards],
            PotionSlots = (int[])state.PotionSlots.Clone(),
            CurrentNodeType = state.CurrentNodeType,
            NeowOptions = (int[])state.NeowOptions.Clone(),
            RewardCards = (int[])state.RewardCards.Clone(),
            RewardGold = state.RewardGold,
            RewardPotion = state.RewardPotion,
            RewardCardPending = state.RewardCardPending,
            PendingOtherCharacterCardRewards = state.PendingOtherCharacterCardRewards,
            NeowAwaitingProceed = state.NeowAwaitingProceed,
            ReturnToRewardScreenAfterCardReward = state.ReturnToRewardScreenAfterCardReward,
            MapNodeTypes = (int[])state.MapNodeTypes.Clone(),
            MapChoices = (int[])state.MapChoices.Clone(),
            ShopCards = (int[])state.ShopCards.Clone(),
            ShopRelics = (int[])state.ShopRelics.Clone(),
            ShopPotions = (int[])state.ShopPotions.Clone(),
            ShopCosts = (int[])state.ShopCosts.Clone(),
            RewardUpgraded = (bool[])state.RewardUpgraded.Clone(),
            RelicReward = state.RelicReward,
            EventId = state.EventId,
            EventValue0 = state.EventValue0,
            EventValue1 = state.EventValue1,
            ActiveCombat = state.ActiveCombat?.Clone(),
            ActiveCombatRng = state.ActiveCombatRng?.Clone(),
            LastPlayerWon = state.LastPlayerWon,
            CompletedCombatRoomsBeforeCurrent = state.CompletedCombatRoomsBeforeCurrent,
            MapNodes = mapNodes,
            CurrentMapCoord = state.CurrentMapCoord,
            MapOptionCoords = ((int Col, int Row)?[])state.MapOptionCoords.Clone(),
            // The act list carries the sequences and the boss for EVERY act, and the
            // per-act properties are views on it — so cloning the list is what clones
            // them. Copying the views instead would silently drop acts 2 and 3.
            Acts =
            [
                .. state.Acts.Select(act =>
                    act with
                    {
                        Events = (int[])act.Events.Clone(),
                        NormalEncounters = (int[])act.NormalEncounters.Clone(),
                        EliteEncounters = (int[])act.EliteEncounters.Clone(),
                    }
                ),
            ],
            CurrentActIndex = state.CurrentActIndex,
            SeaGlassCharacter = state.SeaGlassCharacter,
            NormalEncountersVisited = state.NormalEncountersVisited,
            EliteEncountersVisited = state.EliteEncountersVisited,
            EventSequenceIndex = state.EventSequenceIndex,
            CardRarityOffset = state.CardRarityOffset,
            PotionRewardOdds = state.PotionRewardOdds,
            PendingRelicReward = state.PendingRelicReward,
            ShopRemovalsUsed = state.ShopRemovalsUsed,
            ShopRemovalUsedThisVisit = state.ShopRemovalUsedThisVisit,
            TransformSelectedDeckIndex = state.TransformSelectedDeckIndex,
            PendingSelectionKind = state.PendingSelectionKind,
            PendingSelectionArg = state.PendingSelectionArg,
            PendingSelectionCount = state.PendingSelectionCount,
            PendingOfferCards = (int[])state.PendingOfferCards.Clone(),
            BundleOffer = (int[])state.BundleOffer.Clone(),
            SelectedBundle = state.SelectedBundle,
            PendingOfferPicks = state.PendingOfferPicks,
            PendingOfferOptional = state.PendingOfferOptional,
            EventPage = state.EventPage,
            CrystalSphere = state.CrystalSphere?.Clone(),
            CrystalSphereRng = state.CrystalSphereRng?.Clone(),
            EventRngStream = state.EventRngStream?.Clone(),
            EventRngName = state.EventRngName,
            EventRelicStock = state.EventRelicStock is null ? null : [.. state.EventRelicStock],
            AddingBingBongClone = state.AddingBingBongClone,
            GuiltyCombatsServed = [.. state.GuiltyCombatsServed],
            VisitedEventIds = [.. state.VisitedEventIds],
            PendingSelectionEventEntry = state.PendingSelectionEventEntry,
            PendingGoldRewards = [.. state.PendingGoldRewards],
            PendingCardOffers = [.. state.PendingCardOffers.Select(offer => (int[])offer.Clone())],
            PendingSelectionFollowUpCard = state.PendingSelectionFollowUpCard,
            PendingSelectionFollowUpCount = state.PendingSelectionFollowUpCount,
            PendingSelectionFollowUpHpLoss = state.PendingSelectionFollowUpHpLoss,
            PendingSelectionFollowUpMaxHpGain = state.PendingSelectionFollowUpMaxHpGain,
            PendingSelectionReturnsToEvent = state.PendingSelectionReturnsToEvent,
            PendingSelectionReturn = state.PendingSelectionReturn,
            EventRandomOffer = (int[])state.EventRandomOffer.Clone(),
            TinkerCardType = state.TinkerCardType,
            RelicBag = state.RelicBag.Clone(),
            SharedRelicBag = state.SharedRelicBag.Clone(),
            PendingRestUpgrade = state.PendingRestUpgrade,
            RestResultPending = state.RestResultPending,
            UnknownMapPointsVisited = state.UnknownMapPointsVisited,
            UnknownMapPointMonsterOdds = state.UnknownMapPointMonsterOdds,
            UnknownMapPointEliteOdds = state.UnknownMapPointEliteOdds,
            UnknownMapPointTreasureOdds = state.UnknownMapPointTreasureOdds,
            UnknownMapPointShopOdds = state.UnknownMapPointShopOdds,
            LastResolvedRoomType = state.LastResolvedRoomType,
        };
    }

    /// <summary>
    /// Shuffle the draw pile into an order the agent has not been shown.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The other half of a combat determinization. <see cref="Clone(CombatState, int?)" />
    /// reseeds every stream, which changes every roll STILL TO COME, but the draw order is
    /// not a future roll -- it was materialised by a shuffle that already happened, and a
    /// reseed cannot undo it. Without this, a search that forks the position reads the true
    /// order off its own copy and plays each fight with foresight.
    /// </para>
    /// <para>
    /// Only the UNKNOWN region moves. <see cref="CombatState.KnownTopCount" /> and
    /// <see cref="CombatState.KnownBottomCount" /> exist for exactly this -- cards the
    /// player has been shown, in order, at each end of the pile -- so the shuffle runs
    /// over the middle and leaves both ends where they are. Shuffling through them
    /// resamples information the agent legitimately holds, which is not a determinization
    /// but a corruption: the engine goes on believing the top N are the cards it put
    /// there, and acts on cards that are no longer where it left them.
    /// </para>
    /// <para>
    /// Deliberately not folded into <see cref="Clone(CombatState, int?)" />: a plain clone
    /// is a faithful copy and the differential tests depend on that, seed or no seed.
    /// </para>
    /// </remarks>
    public static void ReshuffleUnknownDrawOrder(this CombatState combat, Random rng)
    {
        int start = combat.KnownTopCount;
        int end = combat.DrawPile.Count - combat.KnownBottomCount;
        // Fisher-Yates over [start, end), the region nothing has been shown.
        for (int i = end - 1; i > start; i--)
        {
            int j = start + rng.Next(i - start + 1);
            (combat.DrawPile[i], combat.DrawPile[j]) = (
                combat.DrawPile[j],
                combat.DrawPile[i]
            );
        }
    }
}
