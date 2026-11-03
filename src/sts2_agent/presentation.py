"""Keep spectator language independent of game actions and native game settings."""

import json
from pathlib import Path


# Older ledgers and configurations retain the existing Chinese presentation.
DEFAULT_LANGUAGE = "zh-CN"
# Only the two reviewed spectator languages are selectable before a run.
PRESENTATION_LANGUAGES = frozenset({DEFAULT_LANGUAGE, "en"})
# This preference applies to fresh runs; a restored ledger owns its language.
PREFERENCE_FILE = "local/presentation-settings.json"
# Localized housekeeping reasons do not affect their associated game actions.
TEXT = {
    "skip_selection": ("跳过当前选牌", "Skip the current card selection"),
    "play_card": ("打出 {name}", "Play {name}"),
    "select_character": ("按开局配置选择 {character}。", "Select {character} as configured for this run."),
    "ascension": ("按开局配置逐步调整至 A{ascension}。", "Adjust difficulty toward A{ascension} as configured."),
    "embark": ("角色和难度已符合开局配置，自动出发。", "Character and difficulty match the settings; embark automatically."),
    "gold_reward": ("自动领取金币奖励。", "Collect the gold reward automatically."),
    "potion_reward": ("药水栏有空位且没有空槽收益取舍，自动领取药水。", "Collect the potion: a slot is free and keeping it empty has no resource tradeoff."),
    "card_reward": ("自动打开卡牌奖励；拿牌或跳过仍由模型决定。", "Open the card reward; the model still decides whether to take a card or skip."),
    "upstream_potion": ("按上游流程自动领取可接收的药水。", "Collect the available potion using the native decision workflow."),
    "rewards_done": ("奖励已处理，自动进入地图。", "Rewards are resolved; proceed to the map automatically."),
    "forced_map": ("地图只有一个可达节点，自动前进。", "Only one map node is reachable; proceed automatically."),
    "shop_done": ("商店购买决策已结束，自动进入地图。", "Shopping decisions are complete; proceed to the map automatically."),
    "open_shop": ("自动打开商店库存供模型比较。", "Open the shop inventory for the model to compare."),
    "proceed": ("当前仅剩前进操作，自动继续。", "Proceed automatically because it is the only remaining action."),
    "end_turn": ("无可用手牌、药水或其他操作，自动结束回合。", "End the turn automatically: no playable cards, potions, or other actions remain."),
    "finish": ("当前仅剩确认或关闭信息界面的操作，自动完成。", "Complete the remaining confirmation or close the information view automatically."),
    "upstream_decision": ("采用上游程序本次合法动作；未调用语言模型。", "Use the decision engine's current legal action without a language-model request."),
}


def language_from_config(config: dict | None) -> str:
    """Use a compatible default when an optional presentation setting is absent."""
    value = (config or {}).get("presentation", {}).get("language", DEFAULT_LANGUAGE)
    return value if isinstance(value, str) and value in PRESENTATION_LANGUAGES else DEFAULT_LANGUAGE


def saved_language(root: Path, fallback: str = DEFAULT_LANGUAGE) -> str:
    """Read an optional preference without writes or effects on a restored run."""
    try:
        value = json.loads((Path(root) / PREFERENCE_FILE).read_text(encoding="utf-8")).get("language")
        return value if isinstance(value, str) and value in PRESENTATION_LANGUAGES else fallback
    except (OSError, ValueError, TypeError, AttributeError):
        return fallback


def display_text(language: str, key: str, **values) -> str:
    """Format one reviewed display string without changing its underlying action."""
    return TEXT[key][1 if language == "en" else 0].format(**values)


def presentation_run_active(view: dict) -> bool:
    """Keep a started run locked even when its native save is temporarily at the menu."""
    if view.get("terminal_evidence"):
        return False
    screen = (view.get("observation") or {}).get("state", {}).get("screen")
    metrics = view.get("metrics", {})
    return (screen not in {None, "MAIN_MENU", "CHARACTER_SELECT"}
            or bool(view.get("budget_started_utc") or metrics.get("requests_used")
                    or metrics.get("actions_completed")))
