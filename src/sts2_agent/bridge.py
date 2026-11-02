"""Loopback bridge with public observations and an isolated Mentor calibration lane."""

from copy import deepcopy
import time

import httpx


# Preserve the existing observation interval until native draw completion can be proven.
SETTLE_POLL_SECONDS = 0.35


class BridgeError(RuntimeError):
    """A safe bridge error with no raw response attached."""

    def __init__(self, code: str):
        """Keep only a short protocol error code for telemetry."""
        self.code = code
        super().__init__(code)


class Bridge:
    """Bind a single game process and issue each game action at most once."""

    def __init__(self, config: dict):
        """Keep local bridge calls off HTTP proxies."""
        self.full_config = config
        self.config = config["bridge"]
        self.client = httpx.Client(base_url=self.config["base_url"], trust_env=False, timeout=45)
        self.identity: tuple | None = None
        self.last_snapshot_metrics: dict = {}
        self.last_settle_metrics: dict = {}
        self.simulation_internal: dict | None = None
        self.simulation_internal_token: str | None = None

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Parse the envelope but never attach raw payloads to exceptions."""
        try:
            response = self.client.request(method, path, **kwargs)
            body = response.json()
        except (httpx.TransportError, ValueError):
            raise BridgeError("transport_outcome_unknown" if method == "POST" else "bridge_unreachable") from None
        if not isinstance(body, dict) or not body.get("ok"):
            code = body.get("error", {}).get("code", "bridge_error") if isinstance(body, dict) else "invalid_envelope"
            raise BridgeError(code if isinstance(code, str) and len(code) < 100 else "bridge_error")
        if not isinstance(body.get("data"), dict):
            raise BridgeError("invalid_envelope")
        return body["data"]

    def health(self) -> dict:
        """Reject another process, another game version, or an active built-in policy."""
        health = self._request("GET", "/health")
        protocol = health.get("external_protocol", {})
        if protocol.get("version") != 1 or not protocol.get("action_receipts"):
            raise BridgeError("guarded_bridge_required_rebuild_mod")
        identity = (health.get("process_id"), protocol.get("instance_id"))
        if type(identity[0]) is not int or identity[0] < 1 or not isinstance(identity[1], str) or not identity[1]:
            raise BridgeError("bridge_identity_missing")
        if self.identity is not None and identity != self.identity:
            raise BridgeError("bridge_process_changed_receipts_lost")
        expected_pid = self.config.get("expected_pid")
        if expected_pid is not None and identity[0] != expected_pid:
            raise BridgeError("wrong_game_pid")
        if health.get("game_version") != self.config["expected_game_version"]:
            raise BridgeError("game_version_changed")
        if health.get("play_running") or health.get("instance_role") != "human":
            raise BridgeError("another_controller_active")
        self.identity = identity
        return {key: deepcopy(health.get(key)) for key in (
            "process_id", "game_version", "mod_version", "status", "api_port", "external_protocol", "compatibility")}

    def snapshot(self) -> tuple[dict, str]:
        """Return public state and bind opt-in simulator data to that exact observation."""
        started = time.monotonic()
        self.simulation_internal = None
        self.simulation_internal_token = None
        try:
            read_access = self.full_config.get("simulation", {}).get("read_access", "public")
            if read_access not in ("public", "internal"):
                raise BridgeError("invalid_simulation_read_access")
            simulation = self.full_config.get("simulation", {})
            mentor = simulation.get("backend", "mentor") == "mentor" and (
                simulation.get("enabled") is True or any(simulation.get(key, "llm") != "llm"
                    for key in ("combat_decision_mode", "run_decision_mode")))
            calibrate = read_access == "internal" or mentor
            kwargs = {"params": {"read_access": "internal"}} if calibrate else {}
            payload = self._request("GET", "/decision-snapshot", **kwargs)
            public = payload.get("public_state")
            token = payload.get("public_observation_token")
            if not isinstance(public, dict) or public.get("protocol_version") != 1 or not isinstance(token, str):
                raise BridgeError("public_protocol_missing")
            if calibrate:
                private = payload.get("simulation_internal")
                if not isinstance(private, dict) or private.get("schema_version") != 1:
                    if read_access == "internal":
                        raise BridgeError("simulation_internal_missing_rebuild_mod")
                    private = None
                if isinstance(private, dict) and private.get("read_access") != "internal":
                    raise BridgeError("invalid_simulation_internal_access")
                if private is not None:
                    self.simulation_internal = deepcopy(private)
                    self.simulation_internal_token = token
            return deepcopy(public), token
        finally:
            # Timing remains controller telemetry; it must not change the public token.
            self.last_snapshot_metrics = {"elapsed_ms": round((time.monotonic() - started) * 1000, 3)}

    def settled(self, timeout: float = 40) -> tuple[dict, str]:
        """Wait for two equal actionable public frames; never choose a waiting action."""
        started = time.monotonic()
        deadline = started + timeout
        previous = None
        last_token = None
        metrics = {"polls": 0, "not_ready_polls": 0, "token_changes": 0, "settled": False}
        try:
            while time.monotonic() < deadline:
                state, token = self.snapshot()
                metrics["polls"] += 1
                if last_token is not None and token != last_token:
                    metrics["token_changes"] += 1
                last_token = token
                if not state.get("supported"):
                    # Scene loading briefly has no screen. Waiting grants no extra policy capability.
                    if state.get("screen") == "UNKNOWN" and not state.get("available_actions"):
                        previous = None
                        metrics["not_ready_polls"] += 1
                        time.sleep(SETTLE_POLL_SECONDS)
                        continue
                    raise BridgeError("unsupported_public_screen:" + str(state.get("screen")))
                readiness = state.get("combat", {}).get("action_readiness", {})
                actionable = state.get("available_actions") or state.get("screen") == "GAME_OVER"
                stable = state.get("screen") != "COMBAT" or (
                    readiness.get("actions_settled", False)
                    and readiness.get("snapshot_stable", False)
                    and readiness.get("can_use_combat_actions", False)
                )
                if actionable and stable and token == previous:
                    metrics["settled"] = True
                    return state, token
                # An unready frame cannot be the first half of a settled pair, even with the same token.
                previous = token if actionable and stable else None
                if not actionable or not stable:
                    metrics["not_ready_polls"] += 1
                time.sleep(SETTLE_POLL_SECONDS)
            raise BridgeError("settled_state_timeout")
        finally:
            metrics["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.last_settle_metrics = metrics

    def execute(self, action: dict, action_id: str, token: str) -> dict:
        """Submit once; reconcile transport loss by receipt, never by repeating POST."""
        self.health()
        command = dict(action)
        command["action"] = command.pop("type")
        command.update(action_id=action_id, expected_observation_token=token)
        try:
            response = self._request("POST", "/action", json=command)
        except BridgeError as error:
            if error.code != "transport_outcome_unknown":
                raise
            return self.receipt(action_id)
        return self._receipt_view(response, action_id)

    def receipt(self, action_id: str) -> dict:
        """Read a receipt from the same process after an ambiguous response."""
        self.health()
        response = self._request("GET", "/action-receipt", params={"action_id": action_id})
        return self._receipt_view(response, action_id)

    @staticmethod
    def _receipt_view(response: dict, action_id: str) -> dict:
        """Copy only public receipt fields; action responses also contain unsafe raw state."""
        return {"action_id": action_id, **{key: deepcopy(response.get(key)) for key in (
            "status", "stable", "public_state", "public_observation_token", "error_code")}}
