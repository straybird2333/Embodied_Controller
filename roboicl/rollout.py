"""RoboICL's single-environment execution loop for an upstream RoboDojo env."""

from __future__ import annotations

import os
import json
from pathlib import Path
import time

import numpy as np

from roboicl.diagnostics import emit
from roboicl.compact_actions import unpack_actions, ActionFormatError
from roboicl.observation_transport import encode_observation
from roboicl.transport import transport_options
from roboicl.policy.action_feedback import TrackingGuard, tracking_receipt
from roboicl.policy.bounded_policy import MAX_RETRY_AFTER_S
from roboicl.policy.camera_readiness import blank_cameras
from roboicl.policy.dialogue_policy import DialogueConfig, load_config
from roboicl.policy.eval_failure import PolicyServiceError, policy_call
from roboicl.policy.head_calibration_capture import capture_head_calibration


def _rpc_timeout_s(config: DialogueConfig) -> float:
    retry_delay = sum(
        max(min(2 ** (attempt - 1), 8), MAX_RETRY_AFTER_S)
        for attempt in range(1, config.max_transport_attempts)
    )
    return (config.max_action_corrections + 1) * (
        config.max_transport_attempts * (config.request_timeout_s + 2) + retry_delay
    ) + 60


def _plain(action: dict) -> dict:
    return {key: np.asarray(value).tolist() for key, value in action.items()}


def _ready_observation(env, *, step: int = 0):
    started = time.monotonic_ns()
    span_id = os.urandom(16).hex()
    emit("span_begin", "camera_ready", source="simulator", span_id=span_id, step=step)
    attempts = []
    for attempt in range(31):
        capture_started = time.monotonic_ns()
        capture_span_id = os.urandom(16).hex()
        emit("span_begin", "observation_capture", source="simulator", span_id=capture_span_id,
             step=step, attempt=attempt + 1)
        try:
            observation = env.get_obs()
        except BaseException as exc:
            emit("span_end", "observation_capture", source="simulator", span_id=capture_span_id,
                 step=step, attempt=attempt + 1,
                 attributes={"duration_seconds": (time.monotonic_ns() - capture_started) / 1e9,
                             "outcome": "error", "exception_type": type(exc).__name__})
            raise
        emit("span_end", "observation_capture", source="simulator", span_id=capture_span_id,
             step=step, attempt=attempt + 1,
             attributes={"duration_seconds": (time.monotonic_ns() - capture_started) / 1e9,
                         "outcome": "ok", "camera_count": len(observation.get("vision", {}))})
        bad = blank_cameras({0: observation})
        if not bad:
            emit("span_end", "camera_ready", source="simulator", span_id=span_id, step=step,
                 attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                             "outcome": "ok", "capture_attempts": attempt + 1,
                             "blank_frame_retries": attempt,
                             "cameras": {key: {"shape": list(value["color"].shape),
                                               "dtype": str(value["color"].dtype)}
                                         for key, value in observation.get("vision", {}).items()
                                         if isinstance(value, dict) and value.get("color") is not None}})
            if step == 0 and not getattr(env, "_roboicl_first_observation_emitted", False):
                env._roboicl_first_observation_emitted = True
                emit("instant", "first_observation_ready", source="simulator", step=step,
                     attributes={"observation_bytes_rgb": sum(int(value["color"].nbytes)
                         for value in observation.get("vision", {}).values()
                         if isinstance(value, dict) and hasattr(value.get("color"), "nbytes")),
                         "cameras": list(observation.get("vision", {}))})
            return observation
        attempts.append({"attempt": attempt, "blank_cameras": bad})
    env._policy_failure = {
        "method": "camera_capture",
        "classification": "camera_capture_failure",
        "official_score": None,
        "attempts": attempts,
    }
    emit("span_end", "camera_ready", source="simulator", span_id=span_id, step=step,
         attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                     "outcome": "error", "attempts": len(attempts),
                     "blank_camera_retry_count": sum(len(row["blank_cameras"]) for row in attempts)})
    raise PolicyServiceError("Camera capture remained blank after 30 render-only retries")


def _chunk_receipt(base_step, step, proposed, reason, last_action=None,
                   feedback=None, rejection_index=None, terminated=False):
    return {"base_step": base_step, "last_step": step,
            "proposed_steps": proposed, "executed_steps": step - base_step,
            "last_executed_action": _plain(last_action) if last_action is not None else None,
            "tracking_feedback": feedback or {}, "rejection_index": rejection_index,
            "discarded_commands": proposed - (step - base_step),
            "reason": reason, "terminated": bool(terminated)}


def _brief_rejection(event):
    # Full IK details stay in the existing server-side continuity log.
    result = {key: event[key] for key in ("type", "rejected_action_executed",
              "action_index", "discarded_commands") if key in event}
    result["violations"] = [{key: value for key, value in item.items()
                              if key in {"arm", "error", "max_current_delta_rad",
                                         "max_previous_delta_rad", "limit_rad"}}
                            for item in event.get("violations", [])]
    return result


def eval_one_episode(env, model_client) -> None:
    """Execute every guarded step locally; publish the negotiated feedback boundaries."""
    config = load_config(os.environ.get("ASTRA_HARNESS_CONFIG"))
    options = transport_options(json.loads(os.environ.get("ROBOICL_TRANSPORT_OPTIONS", "{}")))
    boundary_mode = options["feedback_mode"] == "chunk_boundary"
    from src.eval_client.ik_continuity import IKContinuityError, IKContinuityGuard

    env.ik_continuity_guard = None
    if config.ik_continuity_guard:
        if getattr(env, "num_envs", 1) != 1:
            raise ValueError("IK continuity guard requires one environment per Astra session")
        env.ik_continuity_guard = IKContinuityGuard(
            config.max_joint_increment_rad,
            log_path=Path(env.save_dir) / "_ik_continuity.jsonl")
    if hasattr(model_client, "_client"):
        model_client._client.config.request_timeout_s = _rpc_timeout_s(config)

    def publish(obs):
        policy_call(env, model_client, "update_obs", obs=encode_observation(obs, options))

    policy_call(env, model_client, "reset")
    step = turns = 0
    observation = _ready_observation(env, step=0)
    observation["astra_meta"] = {"step": step, "max_episode_steps": int(env.step_lim)}
    if options != transport_options({}):
        observation["astra_meta"]["transport_options"] = options
    if config.include_head_calibration:
        observation["astra_meta"]["head_calibration_context"] = capture_head_calibration(env, observation)
    publish(observation)

    while not env.is_episode_end():
        if config.max_turns is not None and turns >= config.max_turns:
            raise PolicyServiceError("Harness action dialogue reached max_turns")
        actions = policy_call(env, model_client, "get_action")
        base_step = step
        try:
            if options["action_format"] == "ee_targets_f32_v1":
                actions = unpack_actions(actions, expected_step=base_step,
                                         max_steps=config.execute_horizon)
            elif not isinstance(actions, list) or not 1 <= len(actions) <= getattr(config, "execute_horizon", 64):
                raise ActionFormatError("legacy action result must be a bounded nonempty list")
        except ActionFormatError as exc:
            env._policy_failure = {"method": "get_action", "classification": "policy_service_failure",
                                   "official_score": None, "error_code": exc.error_code}
            candidate = actions.get("targets") if isinstance(actions, dict) else None
            proposed = len(candidate) if isinstance(candidate, np.ndarray) and candidate.ndim == 2 and len(candidate) <= 64 else 0
            event = {"type": "action_payload_rejected", "error_code": exc.error_code,
                     "rejected_action_executed": False, "proposal_size_known": proposed > 0,
                     "received_base_step": actions.get("base_step") if isinstance(actions, dict) and type(actions.get("base_step")) is int else None}
            emit("instant", "action_payload_rejected", source="simulator", step=step,
                 attributes=event)
            if boundary_mode:
                rejected_observation = _ready_observation(env, step=step)
                rejected_observation["astra_meta"] = {
                    "step": step, "max_episode_steps": int(env.step_lim), "controller_event": event,
                    "chunk_receipt": _chunk_receipt(base_step, step, proposed, exc.error_code)}
                publish(rejected_observation)
            raise PolicyServiceError(f"Invalid action response: {exc.error_code}") from None
        turns += 1
        emit("instant", "action_decision", source="simulator", step=step,
             attributes={"decision_count": turns, "base_step": base_step,
                         "proposed_steps": len(actions), "action_format": options["action_format"]})
        tracking = TrackingGuard() if os.environ.get("ASTRA_TRACKING_GUARD", "1") == "1" else None
        last_action = None
        feedback = {}
        reason = "chunk_complete"
        rejection_index = None
        terminated = False
        for action_index, action in enumerate(actions):
            if env.is_episode_end():
                reason = "episode_end"
                terminated = True
                break
            action_started = time.monotonic_ns()
            counters = getattr(env, "_roboicl_diagnostic_counters", {})
            physics_before = counters.get("physics_steps", 0)
            action_span_id = os.urandom(16).hex()
            emit("span_begin", "action_execute", source="simulator", span_id=action_span_id,
                 step=step, attributes={"action_index_in_chunk": action_index})
            try:
                env.take_action(action)
            except IKContinuityError as exc:
                event = dict(exc.event, action_index=action_index,
                             discarded_commands=len(actions) - action_index)
                emit("span_end", "action_execute", source="simulator", span_id=action_span_id,
                     step=step, attributes={"duration_seconds": (time.monotonic_ns()-action_started)/1e9,
                                            "outcome": "rejected", "exception_type": type(exc).__name__,
                                            "physics_steps": counters.get("physics_steps", 0)-physics_before})
                emit("instant", "action_rejected", source="simulator", step=step,
                     attributes={"action_index_in_chunk": action_index,
                                 "completed_steps": step, "reason": "ik_continuity"})
                reason = "ik_rejected"
                rejection_index = action_index
                if boundary_mode:
                    # Rejections consume zero formal steps, but still send a
                    # fresh observation and an explicit prefix/suffix receipt.
                    observation = _ready_observation(env, step=step)
                    observation["astra_meta"] = {
                        "step": step, "max_episode_steps": int(env.step_lim),
                        "controller_event": _brief_rejection(event)}
                else:
                    policy_call(env, model_client, "notify_action_rejection", obs={"step": step, "event": event})
                break
            emit("span_end", "action_execute", source="simulator", span_id=action_span_id,
                 step=step, attributes={"duration_seconds": (time.monotonic_ns()-action_started)/1e9,
                                        "outcome": "ok",
                                        "physics_steps": counters.get("physics_steps", 0)-physics_before})
            step += 1
            last_action = action
            emit("instant", "completed_steps", source="simulator", step=step,
                 attributes={"completed_steps": step})
            observation = _ready_observation(env, step=step)
            observation["astra_meta"] = {"step": step, "max_episode_steps": int(env.step_lim),
                                         "executed_action": _plain(action)}
            feedback = tracking_receipt(action, observation["state"])
            interrupted = tracking is not None and tracking.observe(feedback)
            ended = env.is_episode_end() if boundary_mode else False
            terminated = ended
            if interrupted and action_index+1 < len(actions):
                observation["astra_meta"]["controller_event"] = {
                    "type": "chunk_interrupted_tracking_error",
                    "discarded_commands": len(actions)-action_index-1,
                    "reason": "Two consecutive large arm target residuals; reobserve and replan."}
            if not boundary_mode:
                publish(observation)
            if interrupted or ended:
                reason = "tracking_interrupted" if interrupted else "episode_end"
                break
        if boundary_mode:
            receipt = _chunk_receipt(base_step, step, len(actions), reason, last_action,
                                     feedback, rejection_index, terminated)
            observation["astra_meta"]["chunk_receipt"] = receipt
            emit("instant", "action_chunk_receipt", source="simulator", step=step,
                 attributes=receipt)
            publish(observation)
    policy_call(env, model_client, "finalize")
