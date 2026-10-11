"""Thin adapter that runs one exact layout against the clean RoboDojo submodule."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys
import traceback
import types
import time
import contextvars

from roboicl.paths import ROBODOJO, configure_imports
from roboicl.diagnostics import emit, span

emit("instant", "simulator_python_entry", source="simulator")
from roboicl.remote_policy import install_session_auth, validate_policy_url

configure_imports("sim")

with span("isaac_import", source="simulator"):
    from isaaclab.app import AppLauncher


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--task", required=True)
    value.add_argument("--seed", required=True, type=int)
    value.add_argument("--layout", required=True, type=int)
    value.add_argument("--data-root", required=True, type=Path)
    value.add_argument("--results-root", required=True, type=Path)
    value.add_argument("--port", type=int)
    value.add_argument("--host", default="127.0.0.1")
    value.add_argument("--policy-url")
    value.add_argument("--allow-insecure-policy-loopback", action="store_true")
    value.add_argument("--capture-only", action="store_true")
    value.add_argument("--diagnostic-mode", choices=["first_observation", "fixed_steps"])
    value.add_argument("--max-steps", type=int, default=0)
    AppLauncher.add_app_launcher_args(value)
    return value


class _OfflineClient:
    def __init__(self, *args, **kwargs):
        pass

    def call(self, *, func_name, **kwargs):
        if func_name != "reset":
            raise RuntimeError(f"Capture-only client does not implement {func_name}")
        return None

    def close(self):
        return None


def _install_diagnostic_client(eval_env_module):
    class DiagnosticClient:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, *, func_name, **kwargs):
            if func_name in {"reset", "trial_end", "prepare_case"}:
                return None
            raise RuntimeError(f"diagnostic stub refuses policy call {func_name!r}")

        def close(self):
            return None

    eval_env_module.WsModelClient = DiagnosticClient


def _instrument_env(env):
    counts = {"physics_steps": 0, "render_calls": 0,
              "camera_render_syncs": 0, "observation_captures": 0}
    progress_started_ns = time.monotonic_ns()
    for method, key in (("sim_step", "physics_steps"), ("render", "render_calls")):
        original = getattr(env, method, None)
        if not callable(original):
            continue
        def counted(*args, __original=original, __key=key, **kwargs):
            counts[__key] += 1
            started_ns = time.monotonic_ns()
            if __key == "physics_steps" and counts[__key] % 25 == 0:
                emit("instant", "physics_step_progress", source="simulator",
                     attributes={"physics_steps": counts[__key],
                                 "elapsed_since_instrumentation_seconds": round(
                                     (time.monotonic_ns() - progress_started_ns) / 1e9, 3),
                                 "render_calls": counts["render_calls"],
                                 "camera_render_syncs": counts["camera_render_syncs"],
                                 "observation_captures": counts["observation_captures"]})
            result = __original(*args, **kwargs)
            elapsed = (time.monotonic_ns() - started_ns) / 1e9
            if __key == "physics_steps" and elapsed >= 0.1:
                emit("instant", "slow_physics_step", source="simulator",
                     attributes={"step_index": counts[__key],
                                 "duration_seconds": round(elapsed, 6)})
            return result
        setattr(env, method, counted)
    obs_manager = getattr(env, "obs_manager", None)
    for method, key in (("render_for_capture", "camera_render_syncs"),
                         ("get_obs", "observation_captures")):
        original = getattr(obs_manager, method, None)
        if not callable(original):
            continue
        def counted(*args, __original=original, __key=key, **kwargs):
            counts[__key] += 1
            return __original(*args, **kwargs)
        setattr(obs_manager, method, counted)

    layout = getattr(getattr(env, "scene_manager", None), "layout_manager", None)
    scene_manager = getattr(env, "scene_manager", None)
    manager_methods = [(scene_manager, method, f"scene_{method}")
                       for method in ("reload_scene", "reload_envs", "post_init")]
    for manager_name in ("robot_manager", "camera_manager", "capture_manager",
                         "obs_manager", "reward_manager"):
        manager = getattr(env, manager_name, None)
        if manager is not None and callable(getattr(manager, "reset", None)):
            manager_methods.append((manager, "reset", f"{manager_name}_reset"))
    for manager, method, span_name in manager_methods:
        original = getattr(manager, method, None)
        if not callable(original):
            continue
        def measured_scene_phase(*args, __original=original, __method=method,
                                 __span_name=span_name, __manager=manager, **kwargs):
            started = time.monotonic_ns()
            span_id = os.urandom(16).hex()
            emit("span_begin", __span_name, source="simulator", span_id=span_id,
                 attributes={"environment_count": getattr(__manager, "num_envs", None)})
            try:
                result = __original(*args, **kwargs)
            except BaseException as exc:
                emit("span_end", __span_name, source="simulator", span_id=span_id,
                     attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                                 "outcome": "error", "exception_type": type(exc).__name__})
                raise
            emit("span_end", __span_name, source="simulator", span_id=span_id,
                 attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                             "outcome": "ok"})
            return result
        setattr(manager, method, measured_scene_phase)

    stability_steps = [0]
    warmup = {"started": False, "span_id": None, "start_ns": None, "before": None}
    original_check = getattr(layout, "check_layout_stability", None)
    if callable(original_check):
        def measured_stability(*args, **kwargs):
            start = time.monotonic_ns()
            before_steps = counts["physics_steps"]
            span_id = os.urandom(16).hex()
            emit("span_begin", "layout_stability", source="simulator", span_id=span_id,
                 attributes={"expected_max_physics_steps": 300})
            try:
                result = original_check(*args, **kwargs)
            except BaseException as exc:
                emit("span_end", "layout_stability", source="simulator", span_id=span_id,
                     attributes={"duration_seconds": (time.monotonic_ns() - start) / 1e9,
                                 "outcome": "error", "exception_type": type(exc).__name__,
                                 "physics_steps": counts["physics_steps"] - before_steps})
                raise
            stability_steps[0] = counts["physics_steps"] - before_steps
            emit("span_end", "layout_stability", source="simulator", span_id=span_id,
                 attributes={"duration_seconds": (time.monotonic_ns() - start) / 1e9,
                             "outcome": "ok", "stable": bool(result[0]),
                             "unstable_env_count": len(result[1]),
                             "physics_steps": stability_steps[0]})
            warmup["started"] = True
            warmup["span_id"] = os.urandom(16).hex()
            warmup["start_ns"] = time.monotonic_ns()
            warmup["before"] = dict(counts)
            emit("span_begin", "warmup", source="simulator", span_id=warmup["span_id"])
            return result
        layout.check_layout_stability = measured_stability

    original_setup = getattr(env, "setup_scene", None)
    if callable(original_setup):
        def measured_setup(*args, **kwargs):
            try:
                return original_setup(*args, **kwargs)
            finally:
                if warmup["started"]:
                    delta = {key: counts[key] - warmup["before"][key] for key in counts}
                    emit("span_end", "warmup", source="simulator", span_id=warmup["span_id"],
                         attributes={"duration_seconds": (time.monotonic_ns() - warmup["start_ns"]) / 1e9,
                                     "outcome": "ok", **delta,
                                     "physics_steps_excludes_layout_stability": True})
                    warmup["started"] = False
        env.setup_scene = measured_setup
    env._roboicl_diagnostic_counters = counts
    _instrument_scene_loading(env)
    return counts


def _instrument_scene_loading(env):
    """Expose the individual scene-load calls that can stall during reset.

    Keep this instrumentation in the adapter rather than the RoboDojo
    submodule: it is active only for API diagnostic runs and does not change
    normal evaluation behavior. In particular, a long ``reload_envs`` span
    can now be attributed to room/material setup, stale-object cleanup,
    object creation, or object initialization.
    """
    manager = getattr(env, "scene_manager", None)
    if manager is None:
        return

    # SceneManager receives the inner IsaacRLEnv, whose .sim and .scene are
    # the SimulationContext and InteractiveScene. EvalEnv.sim is a gym wrapper;
    # instrumenting EvalEnv.sim.step would miss reload_envs' direct sim_step.
    first_step = {"pending": False, "active": False, "installed": False}

    def instrument_simulator():
        # EvalEnv defers launch_sim until its first reset. The manager's sim
        # therefore becomes available only after reload_env_scene, not at
        # create_eval_env return.
        if first_step["installed"]:
            return
        simulator = getattr(manager, "sim", None)

        for owner, method, component in (
            (getattr(simulator, "sim", None), "step", "simulation_context_step"),
            (getattr(simulator, "sim", None), "render", "simulation_render"),
            (getattr(simulator, "sim", None), "forward", "simulation_forward"),
            (getattr(simulator, "scene", None), "write_data_to_sim", "scene_write_data_to_sim"),
            (getattr(simulator, "scene", None), "update", "scene_update"),
        ):
            original = getattr(owner, method, None)
            if not callable(original):
                continue

            def measured_component(*args, __original=original, __component=component, **kwargs):
                if first_step["active"]:
                    with span(__component, source="simulator"):
                        return __original(*args, **kwargs)
                started = time.monotonic_ns()
                result = __original(*args, **kwargs)
                elapsed = (time.monotonic_ns() - started) / 1e9
                if elapsed >= 0.1:
                    emit("instant", "slow_sim_component", source="simulator",
                         attributes={"component": __component, "duration_seconds": elapsed})
                return result

            setattr(owner, method, measured_component)

        original_step = getattr(simulator, "sim_step", None)
        if callable(original_step):
            def measured_first_step(*args, **kwargs):
                if not first_step["pending"]:
                    return original_step(*args, **kwargs)
                first_step["pending"] = False
                first_step["active"] = True
                import faulthandler
                faulthandler.dump_traceback_later(60, repeat=True)
                try:
                    with span("first_scene_step", source="simulator",
                              attributes={"simulator_class": type(simulator).__name__,
                                          "python_stack_interval_seconds": 60}):
                        return original_step(*args, **kwargs)
                finally:
                    faulthandler.cancel_dump_traceback_later()
                    first_step["active"] = False
            simulator.sim_step = measured_first_step

        first_step["installed"] = callable(original_step)

    def instrument_pending_initializers():
        instrument_simulator()
        first_step["pending"] = True
        for index, obj in enumerate(getattr(manager, "pending_initialization", ())):
            original = getattr(obj, "initialize", None)
            if not callable(original) or getattr(original, "_roboicl_timed", False):
                continue
            object_name = getattr(obj, "prim_path", None) or getattr(obj, "name", None)

            def measured_initialize(*args, __original=original, __index=index,
                                    __object=obj, __object_name=object_name, **kwargs):
                started = time.monotonic_ns()
                span_id = os.urandom(16).hex()
                attrs = {"initialization_index": __index,
                         "object_type": type(__object).__name__,
                         "object_name": str(__object_name) if __object_name else None}
                emit("span_begin", "scene_object_initialize", source="simulator",
                     span_id=span_id, attributes=attrs)
                try:
                    result = __original(*args, **kwargs)
                except BaseException as exc:
                    emit("span_end", "scene_object_initialize", source="simulator",
                         span_id=span_id,
                         attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                                     "outcome": "error", "exception_type": type(exc).__name__,
                                     **attrs})
                    raise
                emit("span_end", "scene_object_initialize", source="simulator",
                     span_id=span_id,
                     attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                                 "outcome": "ok", **attrs})
                return result

            measured_initialize._roboicl_timed = True
            obj.initialize = measured_initialize

    def wrap(owner, method_name, span_name, attributes_fn=None, after_fn=None):
        original = getattr(owner, method_name, None)
        if not callable(original) or getattr(original, "_roboicl_timed", False):
            return

        def measured(*args, __original=original, __span_name=span_name,
                     __attributes_fn=attributes_fn, **kwargs):
            attrs = __attributes_fn(*args, **kwargs) if __attributes_fn else {}
            started = time.monotonic_ns()
            span_id = os.urandom(16).hex()
            emit("span_begin", __span_name, source="simulator", span_id=span_id,
                 attributes=attrs)
            try:
                result = __original(*args, **kwargs)
            except BaseException as exc:
                emit("span_end", __span_name, source="simulator", span_id=span_id,
                     attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                                 "outcome": "error", "exception_type": type(exc).__name__,
                                 **attrs})
                raise
            if after_fn:
                after_fn()
            emit("span_end", __span_name, source="simulator", span_id=span_id,
                 attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                             "outcome": "ok", **attrs})
            return result

        measured._roboicl_timed = True
        setattr(owner, method_name, measured)

    for method_name in ("reload_background", "reload_lights", "reload_ground", "reload_room",
                        "reload_table", "relocate_stale_objects",
                        "clear_scene_objects", "spawn_scene_objects",
                        "spawn_category_objects"):
        wrap(manager, method_name, f"scene_{method_name}")
    wrap(manager, "reload_env_scene", "scene_reload_env_scene",
         after_fn=instrument_pending_initializers)

    def object_attributes(env_id, asset_to_spawn, prim_path, inst_cfg,
                          default_pos, default_ori, scale):
        # Paths are reduced to a basename to avoid exposing private asset roots.
        asset = str(asset_to_spawn or "")
        return {"env_id": int(env_id), "object_type":
                inst_cfg.get("physics", {}).get("type", "rigid"),
                "asset_name": Path(asset).name if asset else None,
                "prim_path": str(prim_path) if prim_path else None}

    wrap(manager, "create_scene_object", "scene_object_create", object_attributes)


def _run_scene_reuse_experiment(env, args, results_root: Path) -> None:
    """Compare a saved USD stage reload and same-layout object reuse.

    This is deliberately opt-in and only runs for a single diagnostic env. The
    USD stage reopen is a CPU/USD parsing probe; it does not claim to include
    PhysX registration or RTX rendering. The reuse probe performs a second
    reset of the exact same layout in the live environment.
    """
    from pxr import Usd

    run_id = os.environ.get("ROBODOJO_RUN_ID", "scene-reuse-experiment")
    cache_path = results_root / run_id / "scene-cache" / "prepared-scene.usd"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    stage = env.scene_manager.stage
    span_id = os.urandom(16).hex()
    started = time.monotonic_ns()
    emit("span_begin", "scene_file_export", source="simulator", span_id=span_id,
         attributes={"cache_file": str(cache_path)})
    try:
        exported = stage.GetRootLayer().Export(str(cache_path))
        if not exported:
            raise RuntimeError("USD root layer export returned false")
        file_bytes = cache_path.stat().st_size
        emit("span_end", "scene_file_export", source="simulator", span_id=span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                         "outcome": "ok", "file_bytes": file_bytes})
    except BaseException as exc:
        emit("span_end", "scene_file_export", source="simulator", span_id=span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                         "outcome": "error", "exception_type": type(exc).__name__})
        raise

    span_id = os.urandom(16).hex()
    started = time.monotonic_ns()
    emit("span_begin", "scene_file_reopen", source="simulator", span_id=span_id,
         attributes={"file_bytes": file_bytes, "load_all": True})
    try:
        reopened = Usd.Stage.Open(str(cache_path), load=Usd.Stage.LoadAll)
        if reopened is None:
            raise RuntimeError("USD Stage.Open returned no stage")
        prim_count = sum(1 for _ in reopened.Traverse())
        # Release the independent stage before continuing to avoid doubling
        # stage memory during the live simulation.
        reopened = None
        emit("span_end", "scene_file_reopen", source="simulator", span_id=span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                         "outcome": "ok", "prim_count": prim_count,
                         "file_bytes": file_bytes,
                         "scope": "usd_parse_only_no_physx_or_render"})
    except BaseException as exc:
        emit("span_end", "scene_file_reopen", source="simulator", span_id=span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                         "outcome": "error", "exception_type": type(exc).__name__,
                         "scope": "usd_parse_only_no_physx_or_render"})
        raise

    manager = env.scene_manager
    if os.environ.get("ROBOICL_SCENE_REUSE_CONTROL_RESET") == "1":
        span_id = os.urandom(16).hex()
        started = time.monotonic_ns()
        emit("span_begin", "scene_control_reset", source="simulator", span_id=span_id,
             attributes={"layout": args.layout, "seed": args.seed,
                         "mode": "same_process_full_rebuild"})
        try:
            env.reset(seed=[args.layout])
        except BaseException as exc:
            emit("span_end", "scene_control_reset", source="simulator", span_id=span_id,
                 attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                             "outcome": "error", "exception_type": type(exc).__name__})
            raise
        emit("span_end", "scene_control_reset", source="simulator", span_id=span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                         "outcome": "ok", "mode": "same_process_full_rebuild"})

    def inventory():
        rows = []
        for attr in ("_rigid_and_dynamic_objects", "_articulation_objects",
                     "_garment_objects", "_geometry_objects", "_fluid_objects"):
            for env_objects in getattr(manager, attr, []):
                rows.extend((attr, name, getattr(obj, "prim_path", None))
                            for name, obj in env_objects.items())
        return sorted(rows)

    before = inventory()
    original_reload = manager.reload_scene

    def reuse_same_layout():
        # The experiment resets the already loaded, unchanged layout. Updating
        # poses and velocities avoids deleting the prims and reimporting USDs.
        manager.apply_saved_poses(env_idx_list=list(range(manager.num_envs)))

    span_id = os.urandom(16).hex()
    started = time.monotonic_ns()
    emit("span_begin", "scene_reuse_reset", source="simulator", span_id=span_id,
         attributes={"layout": args.layout, "seed": args.seed,
                     "before_object_count": len(before),
                     "reuse_mode": "same_layout_apply_saved_poses"})
    manager.reload_scene = reuse_same_layout
    try:
        env.reset(seed=[args.layout])
    except BaseException as exc:
        emit("span_end", "scene_reuse_reset", source="simulator", span_id=span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                         "outcome": "error", "exception_type": type(exc).__name__})
        raise
    finally:
        manager.reload_scene = original_reload
    after = inventory()
    unchanged = before == after
    emit("span_end", "scene_reuse_reset", source="simulator", span_id=span_id,
         attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                     "outcome": "ok" if unchanged else "inventory_changed",
                     "before_object_count": len(before),
                     "after_object_count": len(after),
                     "same_prim_inventory": unchanged,
                     "mode": "same_layout_apply_saved_poses"})


def _diagnostic_observation(env, *, step=0):
    from roboicl.policy.camera_readiness import blank_cameras
    started = time.monotonic_ns()
    span_id = os.urandom(16).hex()
    emit("span_begin", "camera_ready", source="simulator", span_id=span_id)
    observation = None
    attempts = 0
    bad = []
    for attempts in range(1, 32):
        capture_started = time.monotonic_ns()
        capture_span = os.urandom(16).hex()
        emit("span_begin", "observation_capture", source="simulator", span_id=capture_span,
             step=step, attempt=attempts)
        try:
            observation = env.get_obs()
        except BaseException as exc:
            emit("span_end", "observation_capture", source="simulator", span_id=capture_span,
                 step=step, attempt=attempts,
                 attributes={"duration_seconds": (time.monotonic_ns() - capture_started) / 1e9,
                             "outcome": "error", "exception_type": type(exc).__name__})
            raise
        emit("span_end", "observation_capture", source="simulator", span_id=capture_span,
             step=step, attempt=attempts,
             attributes={"duration_seconds": (time.monotonic_ns() - capture_started) / 1e9,
                         "outcome": "ok", "camera_count": len(observation.get("vision", {}))})
        bad = blank_cameras({0: observation})
        if not bad:
            break
        env.render()
    if bad:
        emit("span_end", "camera_ready", source="simulator", span_id=span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                         "outcome": "error", "blank_camera_count": len(bad), "attempts": attempts})
        raise RuntimeError("camera frames remained blank after 31 captures")
    emit("span_end", "camera_ready", source="simulator", span_id=span_id,
         attributes={"duration_seconds": (time.monotonic_ns() - started) / 1e9,
                     "outcome": "ok", "blank_camera_count": 0, "capture_attempts": attempts,
                     "cameras": {key: {"shape": list(value["color"].shape),
                                       "dtype": str(value["color"].dtype)}
                                 for key, value in observation["vision"].items()
                                 if isinstance(value, dict) and value.get("color") is not None}})
    observation["astra_meta"] = {"step": step, "max_episode_steps": int(env.step_lim)}
    if step == 0:
        emit("instant", "first_observation_ready", source="simulator",
             attributes={"cameras": list(observation.get("vision", {})),
                         "observation_bytes_rgb": sum(int(value["color"].nbytes)
                             for value in observation.get("vision", {}).values()
                             if isinstance(value, dict) and hasattr(value.get("color"), "nbytes")),
                         "task_success": None})
    return observation


def _instrument_policy_rpc():
    """Measure the existing MessagePack codec and RPC round trips in place."""
    try:
        from client_server.ws.protocol import client as protocol_client
    except ImportError:
        return
    size_context = contextvars.ContextVar("roboicl_diag_msgpack_size", default=None)
    original_encoder = protocol_client.encode_frame
    original_request = protocol_client.PolicyEvalClient.request

    def measured_encode(frame):
        started = time.monotonic_ns()
        data = original_encoder(frame)
        elapsed = (time.monotonic_ns() - started) / 1e9
        size_context.set({"message_bytes": len(data), "encode_seconds": elapsed})
        if frame.message_type.value == "call" and frame.payload.get("func_name") == "update_obs":
            obs = frame.payload.get("obs", {})
            vision = obs.get("vision", {}) if isinstance(obs, dict) else {}
            cameras = {}
            pixel_bytes = 0
            if isinstance(vision, dict):
                for camera, camera_data in vision.items():
                    image = camera_data.get("color") if isinstance(camera_data, dict) else None
                    if hasattr(image, "shape") and hasattr(image, "nbytes"):
                        cameras[camera] = {"shape": list(image.shape), "dtype": str(image.dtype),
                                           "pixel_bytes": int(image.nbytes)}
                        pixel_bytes += int(image.nbytes)
                    elif isinstance(image, (bytes, bytearray)):
                        cameras[camera] = {"encoding": "jpeg", "encoded_bytes": len(image)}
            emit("instant", "observation_encode", source="simulator",
                 request_id=getattr(frame, "request_id", None),
                 step=obs.get("astra_meta", {}).get("step") if isinstance(obs, dict) else None,
                 attributes={"message_bytes": len(data), "msgpack_seconds": elapsed,
                             "image_encoding": ("jpeg" if any(v.get("encoding") == "jpeg" for v in cameras.values())
                                                else "raw_rgb_numpy" if cameras else "unknown"),
                             "raw_pixel_bytes": (None if any(v.get("encoding") == "jpeg" for v in cameras.values()) else pixel_bytes),
                             "encoded_color_bytes": sum(v.get("encoded_bytes", 0) for v in cameras.values()),
                             "cameras": cameras})
        return data

    async def measured_request(self, msg_type, payload, *args, **kwargs):
        size_context.set(None)
        started = time.monotonic_ns()
        response = await original_request(self, msg_type, payload, *args, **kwargs)
        elapsed = (time.monotonic_ns() - started) / 1e9
        measured = size_context.get() or {}
        func_name = payload.get("func_name") if isinstance(payload, dict) else None
        if msg_type.value == "call" and func_name == "update_obs":
            emit("instant", "observation_rpc", source="simulator",
                 request_id=getattr(response, "request_id", None),
                 step=payload.get("obs", {}).get("astra_meta", {}).get("step"),
                 attributes={"round_trip_seconds": elapsed,
                             "rpc_inference_step": getattr(response, "step", None),
                             "message_bytes": measured.get("message_bytes"),
                             "send_call_seconds": None,
                             "acknowledgment": "matching RPC response received"})
        elif msg_type.value == "call" and func_name in {"get_action", "get_action_batch"}:
            emit("instant", "policy_action_wait", source="simulator",
                 request_id=getattr(response, "request_id", None),
                 step=getattr(response, "step", None),
                 attributes={"round_trip_seconds": elapsed,
                             "message_bytes": measured.get("message_bytes"),
                             "meaning": "complete policy RPC; not pure LLM inference"})
        return response

    protocol_client.encode_frame = measured_encode
    protocol_client.PolicyEvalClient.request = measured_request


def main() -> int:
    args = parser().parse_args()
    if args.diagnostic_mode == "first_observation" and args.max_steps != 0:
        raise ValueError("first_observation requires --max-steps 0")
    if args.diagnostic_mode == "fixed_steps" and not 1 <= args.max_steps <= 20:
        raise ValueError("fixed_steps requires --max-steps 1..20")
    if os.environ.get("ROBOICL_DIAGNOSTICS_PATH"):
        os.environ["ROBOICL_DIAGNOSTIC_SOURCE"] = "simulator"
    if args.policy_url:
        args.policy_url = validate_policy_url(
            args.policy_url,
            allow_insecure_loopback=args.allow_insecure_policy_loopback,
        )
        install_session_auth(args.policy_url)
        from urllib.parse import urlsplit
        parsed_policy_url = urlsplit(args.policy_url)
        args.host = parsed_policy_url.hostname
        args.port = args.port or parsed_policy_url.port or (
            443 if parsed_policy_url.scheme == "wss" else 80
        )
    elif args.allow_insecure_policy_loopback:
        raise ValueError("--allow-insecure-policy-loopback requires --policy-url")
    if args.port is None:
        raise ValueError("--port is required unless --policy-url is set")
    data_root = args.data_root.expanduser().resolve()
    results_root = args.results_root.expanduser().resolve()
    assets = data_root / "Assets"
    layout = assets / "Eval_Layout/RoboDojo/arx_x5" / str(args.seed) / f"{args.task}_{args.layout}.json"
    if not layout.is_file():
        raise FileNotFoundError(layout)
    results_root.mkdir(parents=True, exist_ok=True)

    # ``env.global_configs`` snapshots these roots at import time.  The
    # launcher normally exports them, but this direct adapter is also a
    # supported entry point (for capture-only and one-layout debugging), so
    # set them before importing any RoboDojo runtime module.  Changing cwd is
    # not sufficient because EVAL_RESULT_PATH is absolute in global_configs.
    os.environ["ROBOICL_DATA_ROOT"] = str(data_root)
    os.environ["ROBOICL_RESULTS_ROOT"] = str(results_root)
    os.chdir(results_root)

    # Configure the clean submodule before any RoboDojo module copies these globals.
    global_configs = importlib.import_module("env.global_configs")
    global_configs.ASSETS_PATH = str(assets)
    global_configs.OBJECTS_PATH = str(assets / "Object/RoboDojo")
    global_configs.ROBOTS_PATH = str(assets / "Robots")

    with span("app_launch", source="simulator"):
        app_launcher = AppLauncher(args)
    simulation_app = app_launcher.app
    env = None
    stage = "import RoboDojo runtime"
    try:
        from roboicl.asset_cache import install_local_asset_aliases
        install_local_asset_aliases(data_root)
        from omegaconf import OmegaConf
        eval_env_module = importlib.import_module("src.eval_client.eval_env")
        _instrument_policy_rpc()
        if args.capture_only:
            # EvalEnv constructs its transport in __init__, before the adapter
            # can replace env.model_client. Inject the no-op client at that one
            # owned boundary so capture-only never waits for a policy server.
            eval_env_module.WsModelClient = _OfflineClient
        elif args.diagnostic_mode:
            _install_diagnostic_client(eval_env_module)
        elif args.policy_url:
            # The gateway starts the policy worker after accepting the
            # authenticated WebSocket handshake. Bound retries so a failed
            # session does not leave the simulator looping for 15 minutes.
            upstream_client = eval_env_module.WsModelClient

            class _RemoteSessionClient(upstream_client):
                def __init__(self, *client_args, **client_kwargs):
                    client_kwargs.setdefault("connect_timeout_s", 10.0)
                    client_kwargs.setdefault("handshake_timeout_s", 300.0)
                    client_kwargs.setdefault("max_connect_attempts", 3)
                    client_kwargs.setdefault("connect_retry_delay_s", 2.0)
                    client_kwargs.setdefault("max_connect_seconds", 30.0)
                    with span("policy_connect", source="simulator",
                              attributes={"connection_role": "simulator_to_gateway",
                                          "retry_limit": client_kwargs["max_connect_attempts"]}):
                        super().__init__(*client_args, **client_kwargs)

            eval_env_module.WsModelClient = _RemoteSessionClient
        create_eval_env = eval_env_module.create_eval_env
        from utils.load_file import load_yaml
        from utils.pipeline_utils import process_config, process_randomization

        registry = importlib.import_module("task.RoboDojo.task_registry")
        stage = "assemble RoboDojo configuration"
        env_cfg_root = ROBODOJO / "env_cfg"
        base = load_yaml(env_cfg_root / "arx_x5.yml")
        eval_cfg = dict(base)
        eval_cfg.update(
            task_name=args.task,
            num_envs=1,
            device_id=0,
            eval_batch=False,
            policy_name="RoboICL",
            additional_info="action_type=ee",
            seed=args.seed,
            physx_monitor_enabled=False,
            eval_num=1,
        )
        deploy_cfg = {
            "policy_name": "RoboICL",
            "port": args.port,
            "host": args.host,
            "protocol": "ws",
            "policy_server_url": args.policy_url or f"ws://{args.host}:{args.port}",
            "evaluation_id": os.environ["ROBODOJO_RUN_ID"],
            "trial_id": f"{args.task}-{os.environ['ROBODOJO_RUN_ID']}",
            "action_case_id": f"{args.task}_case",
            "repeat_index": None,
        }
        task_config = registry.task_config_path(ROBODOJO / "task/RoboDojo/config", args.task)
        env_cfg = OmegaConf.create({
            "sim": load_yaml(env_cfg_root / "sim" / f"{eval_cfg['config']['sim']}.yml"),
            "scene": load_yaml(env_cfg_root / "scene" / f"{eval_cfg['config']['scene']}.yml"),
            "camera": load_yaml(env_cfg_root / "camera" / f"{eval_cfg['config']['camera']}.yml"),
            "robot": load_yaml(env_cfg_root / "robot" / f"{eval_cfg['config']['robot']}.yml"),
            "task_env": load_yaml(task_config),
            "eval_cfg": eval_cfg,
            "deploy_cfg": deploy_cfg,
        })
        env_cfg = process_randomization(env_cfg)
        env_cfg, _ = process_config(env_cfg, task_name=args.task)
        OmegaConf.update(env_cfg, "sim.scene.num_envs", 1, force_add=True)
        OmegaConf.update(env_cfg, "eval_cfg.num_envs", 1, force_add=True)
        env_cfg.sim.seed = [0]
        OmegaConf.update(
            env_cfg,
            "camera.default_frequency",
            eval_cfg["observation"].get("collect_freq", 0),
            force_add=True,
        )
        stage = "create RoboDojo environment"
        with span("env_create", source="simulator"):
            env = create_eval_env(env_cfg, simulation_app)
        counters = _instrument_env(env) if os.environ.get("ROBOICL_DIAGNOSTICS_PATH") else None

        # Upstream currently enumerates matching files and may renumber gaps.
        # Replace that lookup with the exact public layout ID selected by the CLI.
        stage = "bind exact layout"
        env.seed_manager.seed_info = {args.layout: {"scene_layout": str(layout)}}
        env.seed_manager.seed_list = [args.layout]
        env.seed_manager.st_idx = 0
        env.seed_manager.ed_idx = 1
        env.seed_manager.idx = 0
        env.env_seeds = [args.layout]
        if args.capture_only:
            env.model_client = _OfflineClient()
        stage = "reset task scene"
        with span("env_reset", source="simulator"):
            env.reset(seed=[args.layout])

        if (args.diagnostic_mode and
                os.environ.get("ROBOICL_SCENE_REUSE_EXPERIMENT") == "1"):
            _run_scene_reuse_experiment(env, args, results_root)

        if args.capture_only:
            from roboicl.policy.camera_preflight import run
            stage = "capture camera probe"
            run(env)
            return 0

        if args.diagnostic_mode:
            observation = _diagnostic_observation(env)
            if args.diagnostic_mode == "first_observation":
                emit("instant", "diagnostic_complete", source="simulator",
                     attributes={"diagnostic_success": True, "task_success": None,
                                 "completed_steps": 0, "observation_count": 1,
                                 "mode": args.diagnostic_mode})
                return 0

            from roboicl.policy.astra_policy import validate_actions
            from roboicl.policy.dialogue_policy import load_config
            harness = load_config(os.environ.get("ASTRA_HARNESS_CONFIG"))
            from src.eval_client.ik_continuity import IKContinuityGuard, IKContinuityError
            env.ik_continuity_guard = None
            if harness.ik_continuity_guard:
                env.ik_continuity_guard = IKContinuityGuard(
                    harness.max_joint_increment_rad,
                    log_path=Path(env.save_dir) / "_ik_continuity.jsonl")
            completed = 0
            rejected = 0
            for index in range(args.max_steps):
                state = observation["state"]
                action_state = observation.get("action", {})
                def grip(side):
                    value = action_state.get(f"{side}_ee_joint_state", [0.0])
                    return float(value[0] if hasattr(value, "__len__") else value)
                raw_action = {"mode": "ee_delta", "left": [0.0] * 6,
                              "left_gripper": grip("left"), "right": [0.0] * 6,
                              "right_gripper": grip("right")}
                action = validate_actions([raw_action], 1, state)[0]
                action_span = os.urandom(16).hex()
                action_started = time.monotonic_ns()
                emit("span_begin", "action_execute", source="simulator", span_id=action_span,
                     step=index, attributes={"diagnostic_hold_pose": True})
                try:
                    env.take_action(action)
                except IKContinuityError as exc:
                    rejected += 1
                    emit("span_end", "action_execute", source="simulator", span_id=action_span,
                         step=index, attributes={"duration_seconds": (time.monotonic_ns() - action_started) / 1e9,
                                                 "outcome": "rejected", "exception_type": type(exc).__name__})
                    emit("instant", "action_rejected", source="simulator", step=index,
                         attributes={"completed_steps": completed, "reason": "ik_continuity"})
                    break
                completed += 1
                emit("span_end", "action_execute", source="simulator", span_id=action_span,
                     step=index, attributes={"duration_seconds": (time.monotonic_ns() - action_started) / 1e9,
                                             "outcome": "ok"})
                emit("instant", "completed_steps", source="simulator", step=index + 1,
                     attributes={"completed_steps": completed})
                obs_span = os.urandom(16).hex()
                obs_started = time.monotonic_ns()
                emit("span_begin", "observation_capture_cycle", source="simulator", span_id=obs_span,
                     step=index + 1)
                observation = _diagnostic_observation(env, step=index + 1)
                emit("span_end", "observation_capture_cycle", source="simulator", span_id=obs_span,
                     step=index + 1, attributes={"duration_seconds": (time.monotonic_ns() - obs_started) / 1e9,
                                                 "outcome": "ok", "capture_counters": dict(counters)})
            emit("instant", "diagnostic_complete", source="simulator",
                 attributes={"diagnostic_success": True, "task_success": None,
                             "completed_steps": completed, "requested_steps": args.max_steps,
                             "rejection_count": rejected, "observation_count": completed + 1,
                             "mode": args.diagnostic_mode})
            return 0

        from roboicl.rollout import eval_one_episode

        def owned_rollout(instance):
            return eval_one_episode(instance, instance.model_client)

        env.eval_one_episode = types.MethodType(owned_rollout, env)
        stage = "run evaluation"
        env.run_eval()
        return 0
    except BaseException:
        print(f"RoboICL adapter failed during: {stage}", file=sys.stderr, flush=True)
        traceback.print_exc()
        raise
    finally:
        cleanup_start = time.monotonic_ns()
        cleanup_span_id = os.urandom(16).hex()
        emit("span_begin", "cleanup", source="simulator", span_id=cleanup_span_id)
        if env is not None:
            try:
                env.model_client.close()
            except Exception:
                pass
            try:
                env.close()
            except Exception:
                pass
        simulation_app.close()
        emit("span_end", "cleanup", source="simulator", span_id=cleanup_span_id,
             attributes={"duration_seconds": (time.monotonic_ns() - cleanup_start) / 1e9,
                         "outcome": "ok"})


if __name__ == "__main__":
    raise SystemExit(main())
