namespace Sts2Emulator.Core.Effects;

public static class PotionEffects
{
    /// <summary>`Clarity`'s `PowerVar&lt;ClarityPower&gt;(3)` -- TURNS, one card each.</summary>
    private const int ClarityTurns = 3;

    /// <summary>`RadiantTincture`'s `PowerVar&lt;RadiancePower&gt;(3)` -- turns, one energy each.</summary>
    private const int RadianceTurns = 3;

    /// <summary>
    /// `RegenPotion`'s `PowerVar&lt;RegenPower&gt;(5)`. The amount HEALED on the first turn
    /// and the number of turns it runs for, because the power decrements as it pays.
    /// </summary>
    private const int RegenAmount = 5;

    /// <summary>`PowderedDemise`'s `DynamicVar("Demise", 9)`.</summary>
    private const int PowderedDemiseAmount = 9;

    public static void Apply(int potionId, CombatState state, Random? rng = null)
    {
        // Populated incrementally as potions are reverse-engineered from sts2.dll.
        switch (potionId)
        {
            case 5: // Block Potion: gain 12 unpowered Block.
                CardEffects.GainBlock(state, 12, rng);
                break;
            case 9: // Clarity: draw an extra card at the start of each of the next 3 turns.
                BuffSystem.Apply(state.PlayerBuffs, BuffId.Clarity, ClarityTurns);
                break;
            case 17: // Duplicator: play the next card an extra time.
                // `DuplicationPower`, NOT `OneTwoPunchPower`. Both add a play and both
                // decrement, and the game keeps them as separate classes because
                // OneTwoPunch refuses anything that is not an Attack. This case stacked
                // OneTwoPunch, so a Duplicator spent on a Skill or a Power did nothing at
                // all here and doubled it in the game -- see E490.
                BuffSystem.Apply(state.PlayerBuffs, BuffId.Duplication, 1);
                break;
            case 32: // Gigantification: the next attack card deals TRIPLE damage.
                BuffSystem.Apply(state.PlayerBuffs, BuffId.Gigantification, 1);
                break;
            case 47: // Powdered Demise: 9 unblockable to one enemy at each of its turn ends.
                // TargetType.AnyEnemy, so it lands on the AIMED-AT creature rather than on
                // every one of them.
                if (CardEffects.TargetedEnemyFor(state) is { } cursed)
                {
                    BuffSystem.Apply(cursed.Buffs, BuffId.Demise, PowderedDemiseAmount);
                }

                break;
            case 49: // Radiant Tincture: +1 energy for each of the next 3 turns.
                BuffSystem.Apply(state.PlayerBuffs, BuffId.Radiance, RadianceTurns);
                break;
            case 50: // Regen Potion: heal 5 at turn end, winding down by one a turn.
                BuffSystem.Apply(state.PlayerBuffs, BuffId.Regen, RegenAmount);
                break;
            case 51: // Shackling Potion: all enemies lose 7 Strength this turn.
                foreach (var enemy in state.Enemies.Where(e => e.Hp > 0))
                {
                    if (BuffSystem.TryConsumeArtifact(enemy.Buffs))
                    {
                        continue;
                    }

                    BuffSystem.Apply(enemy.Buffs, BuffId.Strength, -7);
                    BuffSystem.Apply(enemy.Buffs, BuffId.TemporaryStrength, 7);
                }
                break;
            default:
                break;
        }
    }
}
