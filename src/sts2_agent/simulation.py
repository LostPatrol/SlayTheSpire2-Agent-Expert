"""Public combat advice over explicitly selected, isolated rule-only JSONL backends."""

from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading
import time

from .public import ActionError, validate_action
from .telemetry import WriterLock, write_json


# The legacy managed backend remains available only through explicit configuration.
HOST_DLL = Path("experiments/combat_simulation/host/bin/Release/net9.0/CombatSimulation.Host.dll")
# Production deployment is independent of task-specific source and build directories.
HOST_MENTOR = Path("local/simulation/mentor-native/mentor_native.pyd")
# The selected rule engine is never silently replaced after a transport or coverage failure.
DEFAULT_BACKEND = "mentor"
# Each host response receives a finite deadline.
DEFAULT_TIMEOUT_SECONDS = 10.0
# Count non-stop actions inside the current player's turn.
DEFAULT_MAX_DEPTH = 6
# Keep the best 64 candidates at each beam depth.
DEFAULT_BEAM_WIDTH = 64
# Exhaustive search runs only below this many terminal strategies.
DEFAULT_EXHAUSTIVE_LIMIT = 20_000
# Explicit legacy planners retain their historical preview-path allowance.
EPISODE_EVALUATION_PATHS = 48
# Search and MCTS model-evaluation calls share durable request admission per episode.
DEFAULT_EPISODE_SEARCH_REQUESTS = 6
# A four-candidate preview leaves half the path allowance for a later replan.
DEFAULT_PREVIEW_SAMPLES = 6
# The explicit legacy evaluation protocol retains its historical candidate limit.
MAX_EVALUATION_CANDIDATES = 4
# The legacy protocol retains its historical sample limit.
MAX_EVALUATION_SAMPLES = 12
# The legacy protocol retains its historical prefix length.
MAX_EVALUATION_ACTIONS = 8
# Only these action shapes may appear in a declared prefix; stop_projection controls a beam, not an action.
SIMULATED_ACTION_FIELDS = {
    "play_card": frozenset({"type", "card_handle", "card_index", "card_id", "target_index"}),
    "use_potion": frozenset({"type", "potion_index", "option_index", "target_index"}),
    "select_card": frozenset({"type", "choice_index"}),
    "select_deck_card": frozenset({"type", "option_index"}),
    "confirm_selection": frozenset({"type"}),
    "skip_selection": frozenset({"type"}),
    "end_turn": frozenset({"type"}),
}
# Automatic rule resolution must also remain bounded within one player action.
MAX_EVALUATION_EVENTS = 512
# These one-action previews expose draw/selection boundaries without searching hypothetical hands.
BOUNDED_PREVIEW_CARDS = frozenset({
    "SHRUG_IT_OFF", "POMMEL_STRIKE", "BATTLE_TRANCE", "OFFERING", "BURNING_PACT",
    "ARMAMENTS", "TRUE_GRIT",
})
# A bounded tail is enough to diagnose a failed child without retaining unlimited stderr.
STDERR_TAIL_LINES = 12
# Keep the raw child tail small; one rotated copy is enough for a post-mortem.
RUNTIME_STDERR_LOG_BYTES = 5_000_000
# Trim retained lines after draining their full contents.
STDERR_LINE_CHARACTERS = 1_000
# Runtime exception classes are diagnostic labels; arbitrary exception text stays local.
SAFE_RUNTIME_ERROR_KINDS = frozenset({
    "Exception", "AssertionError", "AttributeError", "ImportError", "ModuleNotFoundError",
    "IndexError", "KeyError", "MemoryError", "OSError", "OverflowError", "RuntimeError",
    "StopIteration", "TimeoutError", "TypeError", "ValueError", "ZeroDivisionError",
})
# Preserve fixed native search codes without exposing arbitrary state-bearing error text.
SAFE_NATIVE_SEARCH_ERRORS = frozenset({"mcts_search:TerminalRoot"})
# Beam runs first so a slower comparison cannot discard useful primary advice.
STRATEGIES = ("beam", "greedy", "exhaustive")
# Interactive controls are independent of the selected rules backend.
SIMULATION_SETTINGS = {"read_access": "public", "candidate_source": "mcts", "search_strength": 3,
    "combat_decision_mode": "assist", "run_decision_mode": "llm", "rollout_policy": "upstream",
    "action_prior": "upstream", "value_model": "texel", "search_profile": "upstream", "reward_pool": "known"}
# Explicit model plans may be longer than a single initial hand.
MODEL_PLAN_LIMIT = 64
# Evaluation sampling is independent of the legacy 48-path preview allowance.
MODEL_SAMPLE_LIMIT = 256
# Production eval_cheap uses 1000 ordinary / 4000 boss rollouts at the middle setting.
MCTS_SIMULATIONS = (250, 500, 1000, 2500, 5000)
# Match the upstream production rollout horizon rather than the bare library default.
MCTS_ROLLOUT_DEPTH = 200
# This instruction keeps an explanatory sequence out of the real action queue.
ADVICE_USAGE = (
    "The configured decision owner is the final chooser: llm/assist uses the model; upstream uses "
    "the program's current proposal through normal admission without an API request. "
    "Only first_action is executable against this observation. Take at most one action, then "
    "re-observe and recompute; never copy later sequence steps into real actions. "
    "sequence uses simulation-only handles for initial or generated cards, not current hand indices. "
    "MCTS continues supported combat rollouts across turns and includes supported potions. "
    "Wins/losses/cutoffs/visits/mean describe sampled simulator outcomes, not a calibrated live win probability. "
    "Candidate mcts statistics are conditional on the first action; search_mcts pools all adaptive root visits. "
    "MCTS endpoint/state is the immediate first-action or declared-prefix successor, not a final combat state. "
    "Inspect horizon, uncertainty and coverage; unfinished rollouts do not establish victory or safety. "
    "Legacy beam stop_projection only stops a projection and does not settle a turn."
)


class _SimulationFailure(RuntimeError):
    """A transport diagnostic safe to pass through the main decision loop."""

    def __init__(self, code: str, stderr_tail: list[str] | None = None):
        """Keep the short code separate from local-only child diagnostics."""
        super().__init__(code)
        self.code = code
        self.stderr_tail = stderr_tail or []


class CombatSimulator:
    """Keep one optional child alive without choosing or executing game actions."""

    def __init__(self, config: dict, root: str | Path, *, command=None):
        """Configure lazy startup; an injected argv is useful for offline IPC tests."""
        self.root = Path(root).resolve()
        settings = config.get("simulation", {})
        self.backend = settings.get("backend", DEFAULT_BACKEND)
        if self.backend not in {"mentor", "evangambit"}:
            raise ValueError("Unsupported combat simulation backend")
        self._legacy_enabled = (settings.get("enabled", False) is True
                                and config.get("policy", {}).get("deliberation") != "calculation_only")
        self.timeout_seconds = float(settings.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
        self.max_depth = int(settings.get("max_depth", DEFAULT_MAX_DEPTH))
        self.beam_width = int(settings.get("beam_width", DEFAULT_BEAM_WIDTH))
        self.exhaustive_limit = int(settings.get("exhaustive_limit", DEFAULT_EXHAUSTIVE_LIMIT))
        self.episode_search_requests = int(settings.get("episode_search_requests", DEFAULT_EPISODE_SEARCH_REQUESTS))
        self.preview_samples = int(settings.get("preview_samples", DEFAULT_PREVIEW_SAMPLES))
        strategy = settings.get("search_strategy", "mcts" if self.backend == "mentor" else "compare")
        if self.backend == "mentor" and strategy != "mcts" and command is None:
            raise ValueError("Mentor uses upstream MCTS; remove the retired search_strategy setting")
        if strategy not in {*STRATEGIES, "compare", "mcts"}:
            raise ValueError("Unsupported simulation search strategy")
        self.strategies = STRATEGIES if strategy == "compare" else (strategy,)
        self.settings = self.settings_from_config(config)
        self.enabled = self._legacy_enabled or any(self.settings[key] != "llm"
            for key in ("combat_decision_mode", "run_decision_mode"))
        self._internal_state = None
        self._internal_public_hash = None
        self._mirror_encounter = None
        self._unsupported_root = None
        self._command = list(command) if command is not None else None
        self._process = None
        self._lines = None
        self._stderr = deque(maxlen=STDERR_TAIL_LINES)
        # The child hides model-facing detail on purpose; keep its raw tail on disk for diagnosis.
        self._stderr_log = self.root / "local" / "simulation" / "mentor-runtime.stderr.log"
        self._readers = []
        self._lock = threading.Lock()
        self._closed = False
        self._network_identity = {}
        self._rule_identity = self._load_rule_identity()

    def _load_rule_identity(self) -> dict:
        """Fingerprint actual rule artifacts and semantic provenance, never file age or timestamps."""
        if not self.enabled:
            return {"backend": "disabled", "protocol_version": 1}
        artifact = self.root / (HOST_MENTOR if self.backend == "mentor" else HOST_DLL)
        provenance = artifact.with_name("source-provenance.json") if self.backend == "mentor" else (
            self.root / "experiments/combat_simulation/third_party/evangambit/ORIGIN.json")
        try:
            artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError:
            artifact_hash = None
        rule_snapshot = None
        protocol_version = 1
        try:
            source = json.loads(provenance.read_text(encoding="utf-8-sig"))
            if not isinstance(source, dict):
                raise ValueError("Rule provenance must be an object")
            fields = ("protocol_version", "upstream_commit", "commit", "game_version", "backend", "rule_snapshot",
                      "source_hashes", "network_assets",
                      "source_patches", "modified_core_files", "source_modifications",
                      "host_sources", "cargo_lock_sha256", "source_inputs", "runtime", "native_cargo_lock_sha256")
            semantic = {key: source[key] for key in fields if key in source}
            rule_snapshot = source.get("rule_snapshot")
            if type(source.get("protocol_version")) is int:
                protocol_version = source["protocol_version"]
            source_hash = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":"),
                                                   allow_nan=False).encode("utf-8")).hexdigest()
        except (OSError, ValueError, TypeError, UnicodeError):
            source_hash = None
        return {"backend": self.backend, "protocol_version": protocol_version, "rule_snapshot": rule_snapshot,
                "artifact_sha256": artifact_hash, "source_provenance_sha256": source_hash}

    def rule_identity(self) -> dict:
        """Identify the running child's rules, or the artifact that the next child will load."""
        if self._command is None and (self._process is None or self._process.poll() is not None):
            self._rule_identity = self._load_rule_identity()
        if not self.enabled:
            return {"backend": "disabled", "protocol_version": 1}
        return {**deepcopy(self._rule_identity), "settings": deepcopy(self.settings),
                "networks": deepcopy(self._network_identity),
                "search_strategy": list(self.strategies)}

    def network_identity(self) -> dict:
        """Expose only the host-reported loaded models and active component identities."""
        return deepcopy(self._network_identity)

    @staticmethod
    def settings_from_config(config: dict) -> dict:
        """Keep previously disabled configurations disabled until a phase owner explicitly enables them."""
        configured = config.get("simulation", {})
        settings = {key: configured[key] for key in SIMULATION_SETTINGS if key in configured}
        legacy_enabled = configured.get("enabled", False) is True and config.get("policy", {}).get("deliberation") != "calculation_only"
        if not legacy_enabled:
            settings.setdefault("combat_decision_mode", "llm")
            settings.setdefault("run_decision_mode", "llm")
        return CombatSimulator.normalize_settings(settings)

    @staticmethod
    def normalize_settings(settings: dict, base: dict | None = None) -> dict:
        """Migrate legacy candidate ownership while preserving continuous strength and explicit choices."""
        if not isinstance(settings, dict) or set(settings) - set(SIMULATION_SETTINGS):
            raise ValueError("Invalid simulation settings")
        if "candidate_source" in settings and settings["candidate_source"] not in {"model", "mcts"}:
            raise ValueError("Invalid simulation settings")
        merged = {**SIMULATION_SETTINGS, **(base or {}), **settings}
        if "combat_decision_mode" not in settings and "candidate_source" in settings:
            merged["combat_decision_mode"] = "llm" if settings["candidate_source"] == "model" else "assist"
        merged["candidate_source"] = "model" if merged["combat_decision_mode"] == "llm" else "mcts"
        strength = merged["search_strength"]
        if (merged["read_access"] not in {"public", "internal"}
                or any(merged[key] not in {"llm", "assist", "upstream"}
                       for key in ("combat_decision_mode", "run_decision_mode"))
                or merged["rollout_policy"] not in {"upstream", "heuristic"}
                or merged["action_prior"] not in {"upstream", "off"}
                or merged["value_model"] not in {"texel", "learned"}
                or merged["search_profile"] not in {"upstream", "base"}
                or merged["reward_pool"] not in {"known", "full_unlock"}
                or type(strength) not in (int, float) or not 1 <= strength <= 5 or not math.isfinite(strength)):
            raise ValueError("Invalid simulation settings")
        return deepcopy(merged)

    @staticmethod
    def _validate_settings(settings: dict) -> None:
        """Retain the legacy validation entry point for saved controls and offline callers."""
        CombatSimulator.normalize_settings(settings)

    def configure(self, settings: dict) -> None:
        """Apply paused controls while preserving all durable episode accounting."""
        settings = self.normalize_settings(settings, self.settings)
        with self._lock:
            self.settings = deepcopy(settings)
            self.enabled = self._legacy_enabled or any(self.settings[key] != "llm"
                for key in ("combat_decision_mode", "run_decision_mode"))
            if self._process is None or self._process.poll() is not None:
                self._rule_identity = self._load_rule_identity()
            self._network_identity = {}
            self._unsupported_root = None
            self._internal_state = None

    def set_internal_state(self, state: dict, internal: dict | None) -> None:
        """Bind private input to one exact public root without retaining it in model observations."""
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            self._internal_public_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            self._internal_state = deepcopy(internal) if isinstance(internal, dict) else None

    def _request_context(self, observed: dict) -> dict:
        """Supply exact-root calibration privately; read_access controls future search knowledge."""
        context = self._history_context(observed)
        context["read_access"] = self.settings["read_access"]
        context["reward_pool"] = self.settings["reward_pool"]
        if self.backend == "mentor" or self.settings["read_access"] == "internal":
            private = None
            encoded = json.dumps(observed.get("state"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() == self._internal_public_hash:
                private = self._internal_state
            if isinstance(private, dict):
                context["internal_state"] = deepcopy(private)
        if self.backend == "mentor":
            context["mirror_encounter"] = self._mirror_encounter
        return context

    def synchronize(self, observed: dict, *, encounter: str | None, completion: dict | None = None) -> dict:
        """Calibrate the original mirror after settled reality, independently of who chose the action."""
        if self.backend != "mentor" or not self.enabled or self._closed or self._command is not None:
            return {"status": "disabled"}
        with self._lock:
            self._mirror_encounter = encounter
            request = {"op": "mirror_sync", "public_state": observed["state"],
                       **self._request_context(observed)}
            if completion is not None:
                request["completion"] = completion
            try:
                response = self._request(request)
                return response.get("mirror", {"status": "unavailable", "errors": response.get("errors", [])})
            except _SimulationFailure as error:
                return {"status": "unavailable", "code": error.code}

    def _mcts_parameters(self, observed: dict) -> dict:
        """Scale rollout count while retaining the upstream production horizon."""
        state = observed.get("state", {})
        run, combat = state.get("run", {}), state.get("combat", {})
        room = str(run.get("room_type", state.get("room_type", ""))).upper()
        boss = combat.get("is_boss") is True or room in {"BOSS", "BOSS_ROOM", "BOSSROOM"}
        strength = self.settings["search_strength"]
        segment = min(int(strength) - 1, len(MCTS_SIMULATIONS) - 2)
        start, end = MCTS_SIMULATIONS[segment:segment + 2]
        # Round the interpolated simulation count; retain the original continuous setting.
        normal = int(start + (end - start) * (strength - segment - 1) + 0.5)
        return {"simulations": normal * (4 if boss else 1), "normal_simulations": normal,
                "boss_simulations": normal * 4, "max_rollout_depth": MCTS_ROLLOUT_DEPTH,
                **{key: self.settings[key] for key in ("rollout_policy", "action_prior", "value_model", "search_profile",
                                                      "combat_decision_mode", "run_decision_mode")}}

    def _rule_fingerprint(self) -> str:
        """Keep episode accounting continuous while recording every rule revision used."""
        encoded = json.dumps(self.rule_identity(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def advise(self, observed: dict, *, decision_counter: int | None = None,
               decision_counter_source: str | None = None) -> dict:
        """Run configured planners and expose only current-frame first actions to policy."""
        started = time.perf_counter()
        audit = self._audit(observed, results={}, simulation_advice=None)
        if decision_counter is not None:
            if type(decision_counter) is not int or decision_counter < 0:
                raise ValueError("Invalid combat decision counter")
            audit.update(decision_counter=decision_counter, decision_counter_source=decision_counter_source)
        if self.settings["candidate_source"] == "model" and self.enabled:
            audit["diagnostics"].append({"code": "model_candidates_only"})
            audit["simulation_advice"] = {"status": "model_candidates", "usage": ADVICE_USAGE,
                "rule_identity": self.rule_identity(), "settings": deepcopy(self.settings),
                "evaluation_available": self.enabled, "strategies": {},
                "notice": "Automatic search is disabled. Propose candidate prefixes with simulation_evaluate; the model chooses the real action."}
            return audit
        with self._lock:
            state = self._prepare(observed, audit)
            if state is not None:
                for strategy in self.strategies:
                    if not self._reserve_budget(observed, audit, search_requests=1):
                        break
                    request = {
                        "op": "search", "public_state": state, "strategy": strategy,
                        "max_depth": self.max_depth, "beam_width": self.beam_width,
                        "exhaustive_limit": self.exhaustive_limit,
                        **self._request_context(observed),
                    }
                    if strategy == "mcts":
                        request.update(self._mcts_parameters(observed))
                        if decision_counter is not None:
                            request.update(decision_counter=decision_counter, decision_counter_source=decision_counter_source)
                    try:
                        result = self._request(request)
                        self._record_work(observed, result, audit)
                        audit["results"][strategy] = result
                        if (result.get("ok") is False and "public_state_not_supported" in result.get("errors", [])
                                and result.get("support", {}).get("level") == "unsupported"):
                            # Import failures apply to sampled roots too; partial turn gaps do not.
                            self._unsupported_root = (self._root_key(observed, audit), deepcopy(result["support"]))
                            audit["diagnostics"].append({"code": "public_root_import_unsupported",
                                "support": deepcopy(result["support"])})
                            break
                        support = result.get("support", {})
                        blockers = support.get("blocking_reasons") if isinstance(support, dict) else None
                        if (self.backend == "mentor" and isinstance(blockers, list) and blockers
                                and all(isinstance(reason, str) and reason for reason in blockers)):
                            # Mentor imports the root, but apply rejects every state-changing action.
                            self._unsupported_root = (self._root_key(observed, audit), deepcopy(support))
                            audit["diagnostics"].append({"code": "public_root_actions_unsupported",
                                "support": deepcopy(support)})
                            break
                    except _SimulationFailure as failure:
                        audit["diagnostics"].append({"code": failure.code, "strategy": strategy,
                                                     "stderr_tail": failure.stderr_tail})
                        # Retry on the next public decision, never consume a late old response.
                        break
                prompt, usable = self._prompt(observed, audit)
                audit["rule_snapshot"] = next((result.get("rule_snapshot") for result in audit["results"].values()
                                               if result.get("rule_snapshot")), None)
                audit["simulation_advice"] = prompt
                audit["status"] = "partial" if usable else "unavailable"
                if usable and len(audit["results"]) == len(self.strategies) and all(
                    result.get("ok") and not result.get("skipped")
                    and result.get("full_legal_coverage")
                    and result.get("score_support", {}).get("level") == "exact"
                    for result in audit["results"].values()
                ):
                    audit["status"] = "ok"
                if prompt is not None:
                    prompt["status"] = audit["status"]
        rejection = self._known_root_rejection(observed, audit) if state is not None else None
        if rejection:
            action_blocked = bool(rejection.get("blocking_reasons"))
            code = "public_root_actions_unsupported" if action_blocked else "public_root_import_unsupported"
            notice = ("The rule engine imports this public root but blocks every state-changing plan. "
                      "Changing candidate order or names cannot remove these global blockers. " if action_blocked else
                      "This public root cannot be imported by the rule engine. ")
            audit["simulation_advice"] = {"status": "unavailable", "backend": self.backend,
                "comparison_available": False, "support": rejection,
                "evaluation_available": False,
                "notice": notice + "Do not retry simulation_evaluate for this unchanged public root/history/rules; "
                          "it supplies no new evidence. Choose using the current public rules.",
                "bounded_action_previews": {"status": "not_run", "reason": code}}
        if state is not None and not rejection and "mcts" not in self.strategies:
            plans = self._bounded_preview_plans(observed)
            if plans:
                # evaluate owns the same process lock, so dispatch after the search lock is released.
                evaluation = self.evaluate(observed, plans, samples=self.preview_samples, rollout=False)
                audit["bounded_evaluation"] = evaluation
                summary = self._preview_summary(evaluation, plans)
                if audit["simulation_advice"] is None and (evaluation.get("result") or {}).get("ok") is True:
                    audit["simulation_advice"] = {
                        "usage": ADVICE_USAGE, "backend": self.backend, "rule_identity": self.rule_identity(),
                        "public_state_hash": audit["public_state_hash"],
                        "comparison_available": False, "strategies": {},
                        "notice": "Only fixed single-action previews are available; no plan ranking is provided.",
                    }
                    audit["status"] = "partial"
                if audit["simulation_advice"] is not None:
                    audit["simulation_advice"]["bounded_action_previews"] = summary
                    audit["simulation_advice"]["status"] = audit["status"]
        audit["elapsed_ms"] = (time.perf_counter() - started) * 1_000
        return audit

    def advise_run(self, observed: dict, *, public_map: dict | None = None,
                   selection_context: dict | None = None) -> dict:
        """Ask the upstream noncombat policy without executing its returned public action."""
        started = time.perf_counter()
        audit = self._audit(observed, result=None, simulation_advice=None)
        with self._lock:
            if not self.enabled or self._closed:
                audit["diagnostics"].append({"code": "simulation_disabled" if not self.enabled else "simulation_closed"})
            elif self._reserve_budget(observed, audit, search_requests=1):
                request = {"op": "policy_evaluate", "public_state": deepcopy(observed.get("state", {})),
                    "public_map": deepcopy(public_map), "selection_context": deepcopy(selection_context),
                    **self._mcts_parameters(observed), **self._history_context(observed),
                    "read_access": self.settings["read_access"], "reward_pool": self.settings["reward_pool"],
                    "public_uncertainty": {"worlds": 32, "full_unlock_catalogue": self.settings["reward_pool"] == "full_unlock"}}
                try:
                    result = self._request(request)
                    audit["result"] = result
                    self._record_work(observed, result, audit)
                    audit["status"] = "advisory" if result.get("ok") and isinstance(result.get("best_action"), dict) else "unavailable"
                    audit["simulation_advice"] = {"status": audit["status"], "settings": deepcopy(self.settings),
                        "usage": ("Upstream run policy advice; only the current best_action is executable. The configured decision owner chooses it. "
                            "Candidate probabilities describe action selection, not victory; value is a critic scalar, not a calibrated win probability. "
                            "Preserve best_action and selected flags from upstream grouped selection; do not replace them by sorting raw probabilities. "
                            "policy_steps describe proposed local reasoning, not completed game actions. Logical declines remain pending until a native action commits them. "
                            "queued_selection_effects are hypothetical until native confirmation; the observed deck has not yet changed."),
                        **{key: deepcopy(result.get(key)) for key in ("best_action", "candidates", "model_identity",
                            "value", "value_meaning", "value_variance", "value_stddev", "errors", "support",
                            "decision_pipeline", "next_context", "probability_meaning", "grouped_argmax",
                            "logical_declines_pending", "queued_selection_effects")},
                        "policy_steps": self._run_policy_steps_summary(result.get("policy_steps")),
                        "uncertainty": self._run_uncertainty_summary(result.get("uncertainty")),
                        "policy_status": result.get("status")}
                except _SimulationFailure as failure:
                    audit["status"] = "unavailable"
                    audit["diagnostics"].append({"code": failure.code, "stderr_tail": failure.stderr_tail})
        audit["elapsed_ms"] = (time.perf_counter() - started) * 1000
        return audit

    @staticmethod
    def _run_policy_steps_summary(steps: object) -> list | None:
        """Preserve each reward decision and error while leaving full neural observations in the raw audit."""
        if not isinstance(steps, list):
            return None
        summaries = []
        for step in steps:
            if not isinstance(step, dict):
                summaries.append({"status": "invalid", "errors": ["invalid_policy_step"]})
                continue
            summary = {key: deepcopy(step[key]) for key in (
                "ok", "status", "source", "decision_pipeline", "neural_inference", "reward_row",
                "reward_row_i", "reward_type", "relic_id", "potion_id", "selected_upstream_action",
                "native_action", "best_action", "logical_decline", "executed", "actions", "candidates",
                "value", "value_meaning", "value_variance", "value_stddev", "model_identity",
                "grouped_argmax", "probability_meaning", "support", "errors", "next_context") if key in step}
            if "uncertainty" in step:
                summary["uncertainty"] = CombatSimulator._run_uncertainty_summary(step["uncertainty"])
            summaries.append(summary)
        return summaries

    @staticmethod
    def _run_uncertainty_summary(uncertainty: object) -> dict | None:
        """Summarize hypothetical PPO worlds while retaining full raw reports in the linked simulation audit."""
        if not isinstance(uncertainty, dict):
            return None
        worlds = uncertainty.get("worlds")
        worlds = worlds if isinstance(worlds, list) else []
        declared = uncertainty.get("sample_count", len(worlds))
        declared = declared if type(declared) is int and declared >= 0 else len(worlds)
        count = max(declared, len(worlds))
        statuses, errors = Counter(), Counter()
        for world in worlds:
            if not isinstance(world, dict):
                statuses["invalid_world_report"] += 1
                continue
            statuses[str(world.get("status", "unknown"))] += 1
            details = world.get("errors", [])
            details = details if isinstance(details, list) else [details]
            for error in details:
                errors[error if isinstance(error, str) else json.dumps(error, ensure_ascii=False, sort_keys=True)] += 1
        if count > len(worlds):
            statuses["missing_world_report"] = count - len(worlds)
            errors["missing_world_report"] = count - len(worlds)
        completed = statuses["scored"] + statuses["completed"]
        return {"kind": uncertainty.get("kind", "public_uncertainty_mixture"),
            "world_count": count, "completed": completed, "failed": count - completed,
            "status": "complete" if count > 0 and completed == count else "incomplete",
            "status_counts": dict(statuses), "error_counts": dict(errors),
            "assumptions": deepcopy(uncertainty.get("assumptions", [])),
            "reward_pool": uncertainty.get("reward_pool", "known"),
            "aggregation": deepcopy(uncertainty.get("aggregation", "Candidate metrics come from the upstream mixture; incomplete worlds are not silently discarded.")),
            "hypothetical_only": True,
            "notice": "Independent hypothetical worlds are not observed successors. Full obs/probs/value/seeds remain in this decision's original simulation.result.uncertainty.worlds audit."}

    def _root_key(self, observed: dict, audit: dict) -> tuple:
        """Bind import knowledge to all public evidence and the actual rule artifact."""
        history = json.dumps(self._history_context(observed), sort_keys=True, separators=(",", ":"))
        return audit["public_state_hash"], history, self._rule_fingerprint()

    def _known_root_rejection(self, observed: dict, audit: dict) -> dict | None:
        """Reuse a proven global import or action blocker only for identical public evidence."""
        if self._unsupported_root and self._unsupported_root[0] == self._root_key(observed, audit):
            return deepcopy(self._unsupported_root[1])
        return None

    @staticmethod
    def _bounded_preview_plans(observed: dict) -> list[dict]:
        """Fix legal single-action candidates in public hand order before sampling any outcome."""
        plans = []
        hand = observed["state"].get("combat", {}).get("hand", [])
        for row, card in enumerate(hand):
            if card.get("card_id") not in BOUNDED_PREVIEW_CARDS:
                continue
            for target in card.get("targets", []) or [None]:
                action = {"type": "play_card", "card_index": card.get("i")}
                if target is not None:
                    action["target_index"] = target
                try:
                    validate_action(observed, action)
                except (ActionError, KeyError, TypeError):
                    continue
                plans.append({"id": f"hand:{row}:target:{target}", "actions": [action]})
                if len(plans) == MAX_EVALUATION_CANDIDATES:
                    return plans
        return plans

    @classmethod
    def _preview_summary(cls, evaluation: dict, plans: list[dict]) -> dict:
        """Keep at most one illustrative endpoint per candidate and label unfinished samples."""
        candidates = []
        result = evaluation.get("result") or {}
        reports = result.get("candidates", []) if result.get("ok") is True else []
        for plan, candidate in zip(plans, reports):
            samples = candidate.get("samples", [])
            reasons = list(dict.fromkeys(sample.get("stop_reason") for sample in samples
                                         if isinstance(sample.get("stop_reason"), str)))
            item = {
                "id": candidate["id"], "action": plan["actions"][0], "status": candidate["status"],
                "sample_count": candidate.get("sample_count", len(samples)),
                "completed_samples": candidate.get("completed_samples"),
                "metrics": candidate.get("metrics"), "stop_reasons": reasons,
            }
            if (samples and isinstance(samples[0].get("state"), dict)
                    and not samples[0]["state"].get("unsupported_import")):
                endpoint = cls._endpoint(samples[0]["state"])
                if samples[0]["status"] != "completed":
                    endpoint["meaning"] = "partial_resolution_at_reported_stop_not_a_completed_action"
                item["illustrative_sample"] = {
                    "status": samples[0]["status"], "stop_reason": samples[0].get("stop_reason"),
                    "endpoint": endpoint,
                }
            candidates.append(item)
        return {
            "status": evaluation["status"], "backend": evaluation.get("backend"),
            "rule_identity": evaluation.get("rule_identity"), "scope": result.get("scope"),
            "source_provenance": {key: deepcopy(result[key]) for key in (
                "backend", "rule_snapshot", "upstream_commit", "source_modifications") if key in result},
            "rule_snapshot": result.get("rule_snapshot"), "assumptions": result.get("assumptions", []),
            "usage": ("Fixed single-action previews in independent hypothetical worlds, not ranked plans. "
                      "Draws are hypothetical, not predictions of the real next cards. A needs_decision "
                      "or incomplete sample requires another declared choice; it is not zero HP loss. "
                      "Revealed hand instances are omitted; hand_card_counts summarize completed hypothetical "
                      "sample hand quantities, not real draw probabilities or known top cards. Resource metrics "
                      "do not rank draw quality. One illustrative sample is not the expected outcome or a survival guarantee."),
            "budget": evaluation.get("budget"), "diagnostics": evaluation.get("diagnostics", []),
            "candidates": candidates,
        }

    def predict(self, observed: dict, action: dict) -> dict:
        """Project exactly the model's selected action without substituting a search choice."""
        started = time.perf_counter()
        audit = self._audit(observed, action=deepcopy(action), result=None, state=None, support=None)
        with self._lock:
            state = self._prepare(observed, audit)
            if state is not None:
                if not isinstance(action, dict) or action.get("type") not in {"play_card", "use_potion", "end_turn"}:
                    audit["status"] = "unsupported"
                    audit["diagnostics"].append({"code": "action_outside_simulation_scope"})
                elif action.get("type") == "end_turn" and self.backend != "mentor":
                    audit["status"] = "unsupported"
                    audit["diagnostics"].append({"code": "end_turn_is_projection_stop_only"})
                else:
                    try:
                        validate_action(observed, action)
                    except (ActionError, KeyError, TypeError):
                        audit["status"] = "unsupported"
                        audit["diagnostics"].append({"code": "selected_action_invalid_for_observation"})
                    else:
                        try:
                            # The host's card_index is the original public index, not row position.
                            result = self._request({"op": "simulate", "public_state": state,
                                                    "actions": [deepcopy(action)],
                                                    **self._request_context(observed)})
                            audit.update(result=result, state=result.get("state"), support=result.get("support"))
                            audit["rule_snapshot"] = result.get("rule_snapshot")
                            applied = result.get("events", [])
                            audit["status"] = "predicted" if (
                                result.get("ok") is True and len(applied) == 1
                                and applied[0].get("applied") is True
                            ) else "unsupported"
                            if audit["status"] == "predicted" and result.get("hypothetical") is True:
                                audit["status"] = "hypothetical"
                                audit["prediction_kind"] = "sampled_future_from_public_information"
                            stages = result.get("turn_stages")
                            checkpoints = stages.get("checkpoints") if isinstance(stages, dict) else None
                            if (action.get("type") == "end_turn" and isinstance(checkpoints, list)
                                    and checkpoints and all(
                                        isinstance(point, dict) and isinstance(point.get("state"), dict)
                                        and point.get("stage") in {
                                            "player_turn_end_triggers", "player_turn_end", "enemy_turn_start",
                                            "enemy_actions_end", "enemy_turn_end", "next_enemy_intent", "before_next_player_draw"
                                        } for point in checkpoints)):
                                # Checkpoints describe settled stages, never the unknown next hand.
                                audit["deterministic_prefix"] = deepcopy(stages)
                                if audit["status"] != "predicted":
                                    audit["status"] = "partial"
                            if audit["status"] == "unsupported":
                                audit["diagnostics"].append({"code": "selected_action_not_simulated"})
                        except _SimulationFailure as failure:
                            audit["status"] = "unavailable"
                            audit["diagnostics"].append({"code": failure.code, "stderr_tail": failure.stderr_tail})
        if self.backend == "mentor" and isinstance(action, dict) and action.get("type") == "end_turn" and audit["status"] in {"unsupported", "partial"}:
            errors = [item.get("error", "") for item in (audit.get("result") or {}).get("errors", [])]
            # An unknown next hand is a real boundary, not evidence that turn settlement is broken.
            if any(reason == "unknown_draw_boundary" or "public_intent_move_ambiguous" in reason for reason in errors):
                plans = [{"id": "selected_end_turn", "actions": [{"type": "end_turn"}]}]
                evaluation = self.evaluate(observed, plans, samples=DEFAULT_PREVIEW_SAMPLES, max_actions=1, rollout=False)
                summary = self._preview_summary(evaluation, plans)
                audit["uncertainty_evaluation"] = summary
                audit["prediction_kind"] = (
                    "deterministic_turn_prefix_with_independent_hypothetical_continuation"
                    if audit.get("deterministic_prefix")
                    else "independent_hypothetical_worlds_not_known_next_hand"
                )
                candidates = summary.get("candidates", [])
                if (candidates and candidates[0].get("completed_samples") == DEFAULT_PREVIEW_SAMPLES
                        and isinstance(candidates[0].get("metrics"), dict) and candidates[0]["metrics"]):
                    if not audit.get("deterministic_prefix"):
                        audit["status"] = "hypothetical"
                else:
                    audit["diagnostics"].append({"code": "end_turn_hypotheses_incomplete_not_a_prediction"})
        audit["elapsed_ms"] = (time.perf_counter() - started) * 1_000
        return audit

    def evaluate(self, observed: dict, candidate_plans: list[dict], *, samples: int = 12,
                 max_actions: int = MODEL_PLAN_LIMIT, max_events: int = 512, timeout_ms: int = 5_000,
                 scenario_seed: int = 0, rollout: bool = True) -> dict:
        """Evaluate model prefixes and optional combat rollouts in shared hypothetical worlds."""
        started = time.perf_counter()
        audit = self._audit(observed, result=None, budget=None)
        with self._lock:
            state = self._prepare(observed, audit)
            if state is not None:
                rejected = self._known_root_rejection(observed, audit)
                # Empty plans can still inspect an imported root when only actions are blocked.
                action_blocked = bool(rejected and rejected.get("blocking_reasons"))
                has_empty_plan = (isinstance(candidate_plans, list)
                                  and any(isinstance(plan, dict) and plan.get("actions") == []
                                          for plan in candidate_plans))
                if rejected and not (action_blocked and has_empty_plan):
                    audit["status"] = "unsupported"
                    code = "public_root_actions_unsupported" if action_blocked else "public_root_import_unsupported"
                    audit["diagnostics"].append({"code": code, "support": rejected})
                    audit["notice"] = "No new evidence is available from retrying state-changing plans for this unchanged root."
                    audit["elapsed_ms"] = (time.perf_counter() - started) * 1_000
                    return audit
                legacy = not rollout or self.backend == "evangambit"
                action_limit = MAX_EVALUATION_ACTIONS if legacy else MODEL_PLAN_LIMIT
                if legacy and max_actions == MODEL_PLAN_LIMIT:
                    max_actions = MAX_EVALUATION_ACTIONS
                plans = self._evaluation_plans(candidate_plans, max_actions)
                if legacy and plans is not None and len(plans) > MAX_EVALUATION_CANDIDATES:
                    plans = None
                limits = ((samples, 1, MAX_EVALUATION_SAMPLES if legacy else MODEL_SAMPLE_LIMIT),
                          (max_actions, 1, action_limit),
                          (max_events, 1, MAX_EVALUATION_EVENTS), (timeout_ms, 1, 10_000),
                          (scenario_seed, 0, 2_147_483_647))
                rejected = [name for name, (value, low, high) in zip(
                    ("samples", "max_actions", "max_events", "timeout_ms", "scenario_seed"), limits)
                    if type(value) is not int or not low <= value <= high]
                if plans is None or rejected:
                    audit["status"] = "unsupported"
                    detail = (self._plan_rejection(candidate_plans, max_actions) if plans is None
                              else {"reason": "limit_out_of_range", "fields": rejected})
                    audit["diagnostics"].append({"code": "invalid_evaluation_plans_or_limits", **detail})
                elif (self._reserve_evaluation_paths(observed, len(plans) * samples, audit) if legacy else
                      self._reserve_budget(observed, audit, search_requests=1)):
                    request = {
                        "op": "evaluate", "public_state": state, "candidate_plans": plans,
                        "samples": samples, "max_actions": max_actions, "max_events": max_events,
                        "timeout_ms": timeout_ms, "scenario_seed": scenario_seed,
                        "rollout": not legacy, **self._request_context(observed),
                        **self._mcts_parameters(observed),
                    }
                    try:
                        audit["result"] = self._request(request)
                        self._record_work(observed, audit["result"], audit)
                        audit["rule_snapshot"] = audit["result"].get("rule_snapshot")
                        audit["status"] = "evaluated" if audit["result"]["ok"] else "unsupported"
                        if audit["result"].get("ok") is not True:
                            # The child already returned a public error; keep its diagnostics inspectable.
                            audit["diagnostics"].append(self._runtime_diagnostic(audit["result"]))
                    except _SimulationFailure as failure:
                        audit["status"] = "unavailable"
                        audit["diagnostics"].append({"code": failure.code, "stderr_tail": failure.stderr_tail})
        audit["elapsed_ms"] = (time.perf_counter() - started) * 1_000
        return audit

    @staticmethod
    def _evaluation_plans(candidate_plans: list[dict], max_actions: int) -> list[dict] | None:
        """Copy only declared plan data; invalid inputs never silently lose steps or candidates."""
        if (type(max_actions) is not int or not 1 <= max_actions <= MODEL_PLAN_LIMIT
                or not isinstance(candidate_plans, list)
                or not 1 <= len(candidate_plans) <= MODEL_PLAN_LIMIT):
            return None
        identities = set()
        for plan in candidate_plans:
            if not isinstance(plan, dict) or set(plan) != {"id", "actions"}:
                return None
            identity, actions = plan["id"], plan["actions"]
            if (not isinstance(identity, str) or not identity or identity in identities
                    or not isinstance(actions, list) or len(actions) > max_actions
                    or not all(isinstance(action, dict) and isinstance(action.get("type"), str)
                               for action in actions)):
                return None
            for action in actions:
                allowed = SIMULATED_ACTION_FIELDS.get(action["type"])
                if allowed is None or set(action) - allowed:
                    return None
                for field, value in action.items():
                    if field.endswith("_index") and (type(value) is not int or value < 0):
                        return None
                    if field in {"card_handle", "card_id"} and (not isinstance(value, str) or not value):
                        return None
            identities.add(identity)
        try:
            return json.loads(json.dumps(candidate_plans, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, RecursionError):
            return None

    @staticmethod
    def _plan_rejection(candidate_plans: object, max_actions: int) -> dict:
        """Name the first rejected plan or action so a model can correct it instead of retrying blind."""
        if not isinstance(candidate_plans, list) or not candidate_plans:
            return {"reason": "candidate_plans_must_be_a_non_empty_array"}
        if len(candidate_plans) > MODEL_PLAN_LIMIT:
            return {"reason": "too_many_candidate_plans", "limit": MODEL_PLAN_LIMIT,
                    "received": len(candidate_plans)}
        identities = set()
        for plan_index, plan in enumerate(candidate_plans):
            if not isinstance(plan, dict) or set(plan) != {"id", "actions"}:
                return {"reason": "plan_must_have_only_id_and_actions", "plan_index": plan_index}
            identity, actions = plan["id"], plan["actions"]
            if not isinstance(identity, str) or not identity:
                return {"reason": "plan_id_must_be_a_non_empty_string", "plan_index": plan_index}
            if identity in identities:
                return {"reason": "plan_id_must_be_unique", "plan_index": plan_index, "id": identity}
            identities.add(identity)
            if not isinstance(actions, list) or len(actions) > max_actions:
                return {"reason": "plan_actions_must_be_an_array_within_limit", "plan_index": plan_index,
                        "limit": max_actions}
            for position, action in enumerate(actions):
                if not isinstance(action, dict):
                    return {"reason": "action_must_be_an_object", "plan_index": plan_index, "action_index": position}
                kind = action.get("type")
                if not isinstance(kind, str) or not kind:
                    return {"reason": "action_type_missing", "plan_index": plan_index, "action_index": position}
                allowed = SIMULATED_ACTION_FIELDS.get(kind)
                if allowed is None:
                    return {"reason": "action_type_not_simulated", "plan_index": plan_index,
                            "action_index": position, "type": kind,
                            "allowed_types": sorted(SIMULATED_ACTION_FIELDS)}
                extra = sorted(set(action) - allowed)
                if extra:
                    return {"reason": "action_field_not_allowed", "plan_index": plan_index,
                            "action_index": position, "type": kind, "fields": extra}
                for field, value in action.items():
                    if field.endswith("_index") and (type(value) is not int or value < 0):
                        return {"reason": "action_index_must_be_a_non_negative_integer",
                                "plan_index": plan_index, "action_index": position, "field": field}
                    if field in {"card_handle", "card_id"} and (not isinstance(value, str) or not value):
                        return {"reason": "action_handle_must_be_a_non_empty_string",
                                "plan_index": plan_index, "action_index": position, "field": field}
        return {"reason": "unknown_plan_rejection"}

    def _reserve_evaluation_paths(self, observed: dict, requested: int, audit: dict) -> bool:
        """Durably charge paths before dispatch so retries and process restarts cannot refund work."""
        return self._reserve_budget(observed, audit, paths=requested)

    def _record_work(self, observed: dict, result: dict, audit: dict) -> None:
        """Retain measured host work separately from precharged request admission."""
        identity = json.dumps([observed.get("run_id"), observed.get("decision_episode_id")],
                              ensure_ascii=False, separators=(",", ":"))
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        path = self.root / "local/simulation-budgets" / (key + ".json")
        work = result.get("work")
        if not isinstance(work, dict):
            work = {"simulations": (result.get("mcts") or {}).get("visits")}
        try:
            with WriterLock(path.with_suffix(".lock")):
                ledger = json.loads(path.read_text(encoding="utf-8"))
                count = work.get("simulations")
                if type(count) is int and count >= 0:
                    ledger["simulations_used"] = ledger.get("simulations_used", 0) + count
                else:
                    ledger["simulation_work_unknown"] = True
                paths = work.get("paths")
                if type(paths) is int and paths >= 0:
                    ledger["rollout_paths_used"] = ledger.get("rollout_paths_used", 0) + paths
                elapsed = result.get("roundtrip_ms")
                if type(elapsed) in (int, float) and elapsed >= 0:
                    ledger["host_roundtrip_ms"] = ledger.get("host_roundtrip_ms", 0) + elapsed
                write_json(path, ledger)
                audit["work"] = {key: deepcopy(ledger[key]) for key in
                    ("simulations_used", "rollout_paths_used", "simulation_work_unknown", "host_roundtrip_ms") if key in ledger}
        except (OSError, ValueError, RuntimeError):
            audit["diagnostics"].append({"code": "simulation_work_accounting_unavailable"})

    def _reserve_budget(self, observed: dict, audit: dict, *, paths: int = 0, search_requests: int = 0) -> bool:
        """Share one durable episode ledger across backend changes, searches and sampled plans."""
        run_id, episode = observed.get("run_id"), observed.get("decision_episode_id")
        if not isinstance(run_id, str) or not run_id or not isinstance(episode, str) or not episode:
            audit["status"] = "unsupported"
            audit["diagnostics"].append({"code": "evaluation_episode_identity_missing"})
            return False
        identity = json.dumps([run_id, episode], ensure_ascii=False, separators=(",", ":"))
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        path = self.root / "local" / "simulation-budgets" / (key + ".json")
        try:
            with WriterLock(path.with_suffix(".lock")):
                ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
                    "version": 2, "run_id": run_id, "decision_episode_id": episode,
                    "paths_used": 0, "search_requests_used": 0, "rule_fingerprints": [],
                }
                if (not isinstance(ledger, dict) or ledger.get("version") not in {1, 2}
                        or ledger.get("run_id") != run_id or ledger.get("decision_episode_id") != episode
                        or type(ledger.get("paths_used")) is not int or ledger["paths_used"] < 0):
                    raise ValueError("Invalid evaluation budget ledger")
                if ledger["version"] == 1:
                    # Historical search calls were unmetered; preserve known paths without inventing usage.
                    ledger.update(version=2, search_requests_used=0, rule_fingerprints=[],
                                  legacy_search_usage_unknown=True)
                searches = ledger.get("search_requests_used")
                fingerprints = ledger.get("rule_fingerprints")
                if (type(searches) is not int or searches < 0 or not isinstance(fingerprints, list)
                        or not all(isinstance(item, str) for item in fingerprints)):
                    raise ValueError("Invalid shared episode budget ledger")
                used = ledger["paths_used"]
                if paths:
                    audit["budget"] = {"path_limit": EPISODE_EVALUATION_PATHS, "requested_paths": paths,
                                       "paths_used": used, "remaining_paths": max(0, EPISODE_EVALUATION_PATHS - used)}
                if search_requests:
                    audit["search_budget"] = {"request_limit": self.episode_search_requests,
                        "requested_requests": search_requests, "requests_used": searches,
                        "remaining_requests": max(0, self.episode_search_requests - searches)}
                if used + paths > EPISODE_EVALUATION_PATHS or searches + search_requests > self.episode_search_requests:
                    audit["status"] = "budget_exhausted"
                    audit["diagnostics"].append({"code": "evaluation_episode_budget_exhausted" if paths
                                                 else "search_episode_budget_exhausted"})
                    return False
                ledger["paths_used"] = used + paths
                ledger["search_requests_used"] = searches + search_requests
                fingerprint = self._rule_fingerprint()
                if fingerprint not in fingerprints:
                    fingerprints.append(fingerprint)
                ledger["last_rule_identity"] = self.rule_identity()
                write_json(path, ledger)
                if paths:
                    audit["budget"].update(paths_used=used + paths,
                                           remaining_paths=EPISODE_EVALUATION_PATHS - used - paths)
                if search_requests:
                    audit["search_budget"].update(requests_used=searches + search_requests,
                        remaining_requests=self.episode_search_requests - searches - search_requests)
                return True
        except (OSError, RuntimeError, ValueError, UnicodeError):
            audit["status"] = "unavailable"
            audit["diagnostics"].append({"code": "evaluation_budget_unavailable"})
            return False

    def close(self) -> None:
        """Close this client once; no later decision can restart its child."""
        with self._lock:
            self._closed = True
            self._stop_process(graceful=True)

    def _audit(self, observed: dict, **fields) -> dict:
        """Bind local diagnostics to the public observation without copying raw envelopes."""
        return {
            "status": "skipped", "run_id": observed.get("run_id"),
            "observation_id": observed.get("observation_id"),
            "decision_episode_id": observed.get("decision_episode_id"),
            "public_state_hash": None, "backend": self.backend, "rule_identity": self.rule_identity(),
            **self._history_context(observed),
            "diagnostics": [], **fields,
        }

    @staticmethod
    def _history_context(observed: dict) -> dict:
        """Keep witnessed public history separate from the bridge state and its native token."""
        history = observed.get("public_history")
        if not isinstance(history, dict):
            return {}
        try:
            json.dumps(history, allow_nan=False)
        except (TypeError, ValueError):
            return {}
        return {"public_history": deepcopy(history)}

    def _prepare(self, observed: dict, audit: dict) -> dict | None:
        """Accept only a settled public combat frame, without inspecting any raw state."""
        if not self.enabled or self._closed:
            audit["diagnostics"].append({"code": "simulation_disabled" if not self.enabled else "simulation_closed"})
            return None
        state = observed.get("state")
        combat = state.get("combat", {}) if isinstance(state, dict) else {}
        ready = combat.get("action_readiness", {})
        selection_root = (isinstance(state, dict) and state.get("screen") in {"CARD_SELECTION", "MODAL"}
                          and (isinstance(observed.get("public_history", {}).get("combat_selection_origin"), dict)
                               or state.get("selection", {}).get("underlying_screen") == "COMBAT"))
        if not isinstance(state, dict) or (state.get("screen") != "COMBAT" and not selection_root):
            audit["diagnostics"].append({"code": "outside_combat"})
            return None
        if selection_root:
            if state.get("supported") is not True or ready.get("snapshot_stable") is False:
                audit["diagnostics"].append({"code": "public_combat_choice_not_ready"})
                return None
        elif state.get("supported") is not True or not all(ready.get(key) is True for key in (
            "actions_settled", "snapshot_stable", "can_use_combat_actions",
        )) or ready.get("modal_open") is True or ready.get("is_paused") is True:
            audit["diagnostics"].append({"code": "public_combat_not_ready"})
            return None
        try:
            encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError):
            audit["status"] = "unavailable"
            audit["diagnostics"].append({"code": "public_state_not_json"})
            return None
        audit["public_state_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return deepcopy(state)

    def _start_process(self) -> None:
        """Create separate response queues for each process generation."""
        if self._process is not None and self._process.poll() is None:
            return
        self._stop_process(graceful=False)
        command = self._command
        if command is None:
            if self.backend == "mentor":
                host = self.root / HOST_MENTOR
                if not host.is_file():
                    raise _SimulationFailure("simulation_host_missing")
                # Popen's encoding covers parent pipes only; Windows child JSON must also use UTF-8.
                command = [str(self.root / ".venv/Scripts/python.exe"), "-X", "utf8", "-m", "sts2_agent.mentor_runtime",
                           "--deployment", str(host.parent)]
            else:
                runtime, host = self.root / ".tools/dotnet/dotnet.exe", self.root / HOST_DLL
                if not runtime.is_file() or not host.is_file():
                    raise _SimulationFailure("simulation_host_missing")
                command = [str(runtime), str(host)]
        lines = Queue()
        stderr = deque(maxlen=STDERR_TAIL_LINES)
        try:
            process = subprocess.Popen(
                command, cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, ValueError):
            raise _SimulationFailure("simulation_host_start_failed") from None
        self._process, self._lines, self._stderr = process, lines, stderr
        self._readers = [
            threading.Thread(target=self._read_stdout, args=(process, lines), daemon=True),
            threading.Thread(target=self._read_stderr, args=(process, stderr), daemon=True),        ]
        for reader in self._readers:
            reader.start()

    @staticmethod
    def _read_stdout(process, lines) -> None:
        """Forward complete JSON lines to this generation's response queue."""
        try:
            for line in process.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    def _read_stderr(self, process, stderr) -> None:
        """Drain the child pipe, keep a bounded tail and mirror it to a rotating local log."""
        try:
            log = self._stderr_log
            log.parent.mkdir(parents=True, exist_ok=True)
            if log.is_file() and log.stat().st_size > RUNTIME_STDERR_LOG_BYTES:
                log.replace(log.with_suffix(log.suffix + ".1"))
            with log.open("a", encoding="utf-8") as handle:
                for line in process.stderr:
                    text = line.rstrip()[:STDERR_LINE_CHARACTERS]
                    stderr.append(text)
                    if text:
                        handle.write(text + "\n")
                        handle.flush()
        except (OSError, ValueError):
            pass

    def _runtime_diagnostic(self, result: dict) -> dict:
        """Publish the child's failure label and a bounded stderr tail, never the raw reason text."""
        errors = result.get("errors")
        kind = result.get("error_kind")
        return {"code": "simulation_runtime_error",
                "error_kind": kind if isinstance(kind, str) else None,
                "errors": deepcopy(errors) if isinstance(errors, list) else [],
                "stderr_tail": list(self._stderr),
                "stderr_log": str(self._stderr_log)}

    def _request(self, request: dict) -> dict:
        """Serialize one request and kill failed hosts before the next request can run."""
        started = time.perf_counter()
        self._start_process()
        try:
            self._process.stdin.write(json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n")
            self._process.stdin.flush()
            line = self._lines.get(timeout=self.timeout_seconds)
            if line is None:
                raise _SimulationFailure("simulation_host_exited")
            try:
                result = json.loads(line)
            except (ValueError, TypeError):
                raise _SimulationFailure("simulation_host_invalid_json") from None
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise _SimulationFailure("simulation_host_invalid_response")
            if self._command is None and self.backend == "mentor" and (
                result.get("backend") != "mentor" or not isinstance(result.get("rule_snapshot"), str)
                or not result["rule_snapshot"]
            ):
                raise _SimulationFailure("simulation_host_backend_identity_missing")
            self._check_response(request, result)
            for key in ("model_identity", "network_identity", "components"):
                if isinstance(result.get(key), dict):
                    self._network_identity[key] = deepcopy(result[key])
            result.setdefault("backend", self.backend)
            result["rule_identity"] = self.rule_identity()
            result["roundtrip_ms"] = (time.perf_counter() - started) * 1_000
            return result
        except (OSError, ValueError, Empty, _SimulationFailure) as error:
            code = (error.code if isinstance(error, _SimulationFailure) else
                    "simulation_host_timeout" if isinstance(error, Empty) else "simulation_host_io_failed")
            self._stop_process(graceful=False)
            raise _SimulationFailure(code, list(self._stderr)) from None

    @staticmethod
    def _check_response(request: dict, result: dict) -> None:
        """Reject malformed success payloads before optional advice can break the main loop."""
        if not result["ok"] or result.get("skipped") is True:
            return
        if request["op"] == "policy_evaluate":
            action = result.get("best_action")
            if action is not None and (not isinstance(action, dict) or not isinstance(action.get("type"), str)):
                raise _SimulationFailure("simulation_host_invalid_response")
            return
        if request["op"] == "evaluate":
            candidates = result.get("candidates")
            expected = [plan["id"] for plan in request["candidate_plans"]]
            if (not isinstance(candidates, list) or len(candidates) != len(expected)
                    or not all(isinstance(candidate, dict) for candidate in candidates)
                    or [candidate.get("id") for candidate in candidates] != expected):
                raise _SimulationFailure("simulation_host_invalid_response")
            for candidate in candidates:
                samples = candidate.get("samples")
                if (not isinstance(samples, list) or len(samples) != request["samples"]
                        or not all(isinstance(sample, dict) and isinstance(sample.get("status"), str)
                                   for sample in samples)
                        or not isinstance(candidate.get("status"), str)):
                    raise _SimulationFailure("simulation_host_invalid_response")
                completed = sum(sample["status"] == "completed" for sample in samples)
                if (type(candidate.get("sample_count")) is not int or candidate["sample_count"] != len(samples)
                        or type(candidate.get("completed_samples")) is not int
                        or candidate["completed_samples"] != completed
                        or (completed != len(samples) and candidate.get("metrics") is not None)):
                    raise _SimulationFailure("simulation_host_invalid_response")
            try:
                json.dumps(result, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise _SimulationFailure("simulation_host_invalid_response") from None
            return
        if request["op"] == "simulate":
            events = result.get("events")
            if not isinstance(result.get("state"), dict) or not isinstance(events, list) or not all(
                isinstance(event, dict) for event in events
            ):
                raise _SimulationFailure("simulation_host_invalid_response")
            state = result["state"]
        else:
            best = result.get("best")
            if best is None:
                return
            if not isinstance(best, dict) or not isinstance(best.get("actions"), list) or not all(
                isinstance(action, dict) for action in best["actions"]
            ) or not isinstance(best.get("state"), dict):
                raise _SimulationFailure("simulation_host_invalid_response")
            score = best.get("score")
            if score is not None and (type(score) not in (int, float) or not math.isfinite(score)):
                raise _SimulationFailure("simulation_host_invalid_response")
            state = best["state"]
        if not isinstance(state.get("player", {}), dict) or not isinstance(state.get("enemies", []), list) or not all(
            isinstance(enemy, dict) for enemy in state.get("enemies", [])
        ):
            raise _SimulationFailure("simulation_host_invalid_response")

    def _stop_process(self, *, graceful: bool) -> None:
        """Reap the host and its pipe readers without preserving a stale response queue."""
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                if graceful:
                    try:
                        process.stdin.close()
                        process.wait(timeout=0.5)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        process.kill()
                else:
                    process.kill()
                process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for reader in self._readers:
            reader.join(timeout=0.2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        self._process, self._lines, self._readers = None, None, []

    def _prompt(self, observed: dict, audit: dict) -> tuple[dict | None, int]:
        """Summarize results while keeping later simulated handles non-executable."""
        summaries, usable, no_comparisons = {}, 0, 0
        hand = observed["state"].get("combat", {}).get("hand", [])
        for strategy in self.strategies:
            result = audit["results"].get(strategy)
            if result is None:
                summaries[strategy] = {"status": "unavailable", "reason": "host_request_not_completed"}
                continue
            if not result.get("ok") or result.get("skipped"):
                errors = result.get("errors")
                runtime_failed = isinstance(errors, list) and "upstream_mirror_request_failed" in errors
                search_errors = ([error for error in errors
                                  if isinstance(error, str) and error in SAFE_NATIVE_SEARCH_ERRORS]
                                 if isinstance(errors, list) else [])
                # The runtime already redacts truth-board exceptions into this fixed protocol code.
                reason = ("upstream_mirror_request_failed" if runtime_failed else
                          result.get("reason") or (search_errors[0] if search_errors else "no_supported_plan"))
                details = {"reason": reason}
                if search_errors:
                    details["errors"] = search_errors
                if runtime_failed:
                    details["errors"] = ["upstream_mirror_request_failed"]
                    error_kind = result.get("error_kind")
                    if isinstance(error_kind, str) and error_kind in SAFE_RUNTIME_ERROR_KINDS:
                        details["error_kind"] = error_kind
                    # The child hides raw reasons; keep its own tail for the post-mortem.
                    details["stderr_tail"] = list(self._stderr)
                    details["stderr_log"] = str(self._stderr_log)
                summaries[strategy] = {"status": "skipped" if result.get("skipped") else "unsupported", **details}
                audit["diagnostics"].append({"code": reason if runtime_failed else "strategy_without_plan",
                                             "strategy": strategy, **details})
                continue
            best = result.get("best") or {}
            actions = best.get("actions", [])
            if not actions:
                summaries[strategy] = {"status": "unsupported", "reason": "no_supported_plan"}
                audit["diagnostics"].append({"code": "strategy_without_plan", "strategy": strategy, "reason": "no_supported_plan"})
                continue
            comparison_available = result.get("comparison_available", strategy == "mcts" or any(
                action.get("type") in {"play_card", "use_potion"} for action in actions))
            if comparison_available is not True and strategy != "mcts":
                no_comparisons += 1
                summaries[strategy] = {
                    "status": "unavailable", "reason": "no_supported_card_play_comparison",
                    "support": result.get("support"),
                    "search_support_reasons": result.get("search_support_reasons", []),
                }
                audit["diagnostics"].append({"code": "no_supported_card_play_comparison", "strategy": strategy})
                continue
            try:
                first = self._first_action(actions[0], hand)
                validate_action(observed, first)
                sequence = [({"type": "end_turn"} if strategy == "mcts" and action.get("type") == "end_turn"
                             else self._sequence_step(action, hand)) for action in actions]
            except (ActionError, KeyError, IndexError, TypeError, ValueError):
                summaries[strategy] = {"status": "unsupported", "reason": "first_action_not_currently_legal"}
                audit["diagnostics"].append({"code": "invalid_simulated_plan", "strategy": strategy})
                continue
            usable += 1
            summaries[strategy] = {
                "status": "advisory", "first_action": first, "sequence": sequence,
                "comparison_available": comparison_available,
                "backend": result.get("backend", self.backend), "rule_snapshot": result.get("rule_snapshot"),
                "score": best.get("score"), "score_breakdown": best.get("score_breakdown"),
                "mcts": deepcopy(best.get("mcts", result.get("mcts"))),
                "decision_identity": deepcopy(result.get("decision_identity")),
                "model_retains_final_choice": result.get("model_retains_final_choice"),
                "forced_public_action": result.get("forced_public_action", False),
                "search_mcts": deepcopy(result.get("mcts")),
                "statistics_scope": "conditional_on_first_action" if strategy == "mcts" else None,
                "limits": deepcopy(result.get("limits")), "scope": result.get("scope"),
                "evaluation": deepcopy(best.get("evaluation")),
                "alternatives": self._alternatives(result.get("alternatives", []), hand,
                                                    limit=None if strategy == "mcts" else 3),
                "support": result.get("support"), "score_support": best.get("score_support", result.get("score_support")),
                "full_legal_coverage": result.get("full_legal_coverage", False),
                "search_support_reasons": result.get("search_support_reasons", []),
                "optimal_within_supported_horizon": result.get("optimal_within_supported_horizon", False),
                "termination": best.get("termination"),
                "endpoint": self._endpoint(best.get("state", {})),
            }
            if strategy == "mcts":
                summaries[strategy]["endpoint"]["horizon"] = "after_first_action_before_combat_rollouts"
                summaries[strategy]["endpoint"]["meaning"] = "immediate_first_action_successor_not_final_combat_outcome"
        if not usable and not no_comparisons:
            return None, 0
        return {
            "usage": ADVICE_USAGE, "backend": self.backend, "rule_identity": self.rule_identity(),
            "settings": deepcopy(self.settings),
            "decision_owner": self.settings["combat_decision_mode"],
            "source_provenance": {key: deepcopy(value) for result in list(audit["results"].values())[:1]
                                  for key, value in result.items() if key in {
                                      "backend", "rule_snapshot", "upstream_commit", "source_modifications"}},
            "search_budget": deepcopy(audit.get("search_budget")),
            "public_state_hash": audit["public_state_hash"],
            "comparison_available": any(item.get("status") == "advisory" and item.get("comparison_available") is True
                                        for item in summaries.values()),
            "notice": (None if usable else "Unable to compare card plays in the supported simulation domain. "
                       "A stop-only projection is not an end-turn recommendation or strategy validation. "
                       "Choose from the public legal actions using their visible rules."),
            "limits": (deepcopy(audit["results"].get("mcts", {}).get("limits", {})) if "mcts" in self.strategies
                       else {"max_depth": self.max_depth, "beam_width": self.beam_width,
                             "exhaustive_limit": self.exhaustive_limit}),
            "strategies": summaries,
        }, usable

    @classmethod
    def _alternatives(cls, alternatives: list, hand: list, *, limit: int | None = 3) -> list[dict]:
        """Preserve distinct candidate metrics while keeping later handles explanatory only."""
        summaries = []
        for candidate in alternatives[:limit]:
            try:
                actions = candidate["actions"]
                summaries.append({"first_action": cls._first_action(actions[0], hand),
                    "sequence": [({"type": "end_turn"} if limit is None and action.get("type") == "end_turn"
                                  else cls._sequence_step(action, hand)) for action in actions],
                    "evaluation": deepcopy(candidate.get("evaluation")), "score": candidate.get("score"),
                    "mcts": deepcopy(candidate.get("mcts"))})
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return summaries

    @staticmethod
    def _endpoint(state: dict) -> dict:
        """Expose resources at the reported horizon without relabeling sampled turn ends."""
        player = state.get("player", {})
        return {
            "meaning": ("after_hypothetical_end_turn_resolution" if state.get("end_turn_executed")
                        else "after_supported_card_plays_before_turn_end_or_enemy_actions"),
            "horizon": state.get("horizon", "current_player_turn_before_end_turn_effects"),
            "player": {key: deepcopy(player[key]) for key in ("hp", "block", "energy", "stars", "powers")
                       if key in player},
            "enemies": [{key: deepcopy(enemy[key]) for key in ("i", "enemy_id", "hp", "block", "powers", "alive")
                         if key in enemy} for enemy in state.get("enemies", [])],
        }

    @staticmethod
    def _hand_row(action: dict, hand: list) -> int:
        """Resolve instances by their input-row handle and cross-check the public index."""
        handle = action.get("card_handle")
        if not isinstance(handle, str) or not handle.startswith("h") or not handle[1:].isdigit():
            raise ValueError("invalid_hand_handle")
        row = int(handle[1:])
        card = hand[row]
        if action.get("card_index") is not None and action["card_index"] != card.get("i"):
            raise ValueError("inconsistent_hand_handle")
        return row

    @classmethod
    def _first_action(cls, action: dict, hand: list) -> dict:
        """Build the sole action that may use indices from the current public frame."""
        if action.get("type") == "end_turn":
            return {"type": "end_turn"}
        if action.get("type") in {"use_potion", "select_card", "select_deck_card", "confirm_selection", "skip_selection"}:
            result = {key: value for key, value in action.items()
                    if key in {"type", "potion_index", "option_index", "target_index", "card_index"}
                    and value is not None}
            if action["type"] == "use_potion" and "potion_index" in result:
                result["option_index"] = result.pop("potion_index")
            return result
        if action.get("type") != "play_card":
            raise ValueError("unknown_simulated_action")
        card = hand[cls._hand_row(action, hand)]
        result = {"type": "play_card", "card_index": card["i"]}
        if action.get("target_index") is not None:
            result["target_index"] = action["target_index"]
        return result

    @classmethod
    def _sequence_step(cls, action: dict, hand: list) -> dict:
        """Describe later plays without exposing stale executable card indices."""
        if action.get("type") == "end_turn":
            return {"type": "stop_projection"}
        if action.get("type") in {"use_potion", "select_card", "select_deck_card", "confirm_selection", "skip_selection"}:
            return deepcopy(action)
        if action.get("type") != "play_card":
            raise ValueError("unknown_simulated_action")
        handle = action.get("card_handle")
        if isinstance(handle, str) and handle.startswith("s") and handle[1:].isdigit():
            if action.get("card_index") is not None or not isinstance(action.get("card_id"), str) or not action["card_id"]:
                raise ValueError("invalid_generated_card_handle")
            # Generated instances exist only in the projection and cannot identify a real hand slot.
            result = {"type": "play_card", "card_handle": handle, "card_id": action["card_id"],
                      "origin": "simulated_generated_card"}
            if action.get("target_index") is not None:
                result["target_index"] = action["target_index"]
            return result
        row = cls._hand_row(action, hand)
        card = hand[row]
        result = {"type": "play_card", "card_handle": action["card_handle"],
                  "initial_hand_row": row, "card_id": card.get("card_id"),
                  "upgraded": card.get("upgraded", False)}
        if action.get("target_index") is not None:
            result["target_index"] = action["target_index"]
        return result
