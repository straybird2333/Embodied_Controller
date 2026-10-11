"""Bounded, no-LLM real-simulator transport smoke via the local deployed API.

Run with the policy interpreter on the RTX server. All control and external
policy connections use loopback; the API's simulator gateway must also be
loopback. Requires the deployed services, their local key file and task assets.
Cancels after five accepted holds; no task score or public throughput claim.
"""
import asyncio
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roboicl.paths import configure_imports
configure_imports("policy")
import numpy as np
from websockets.asyncio.client import connect
from client_server.ws.model_server import PolicyServer
from client_server.ws.protocol.codec import decode_envelope, encode_frame
from roboicl.compact_actions import pack_actions
from roboicl.transport import transport_options


def service_env(name):
    pid = subprocess.check_output(["systemctl", "show", name, "--property=MainPID", "--value"], text=True).strip()
    return dict(item.decode().split("=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in item)


async def exercise(options, env, gateway_env):
    key = Path(env["ROBOICL_ACCESS_KEY_FILE"]).read_text().strip()
    api = "http://127.0.0.1:" + env.get("ROBOICL_API_PORT", "8080")
    def request(path, body=None):
        req = urllib.request.Request(api + path,
            data=None if body is None else json.dumps(body, separators=(",", ":")).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    body = {"task": "deposit_coin", "shots": 0, "seed": 0, "layout": 0,
            "profile": "roboicl-0shot-b25-v1"}
    session = await asyncio.to_thread(request, "/v1/policy-sessions", body | options)
    for name, value in options.items():
        assert session[name] == value
    updates, messages = [], []
    class HoldModel:
        obs = None
        decisions = 0
        def reset(self): pass
        def prepare_case(self, obs): pass
        def update_obs(self, obs):
            self.obs = obs
            step = obs["astra_meta"]["step"]
            for camera in obs["vision"].values():
                color = camera["color"]
                assert isinstance(color, np.ndarray) and color.shape == (480, 640, 3) and color.dtype == np.uint8
            updates.append({"step": step, "chunk_receipt": obs["astra_meta"].get("chunk_receipt")})
            print(json.dumps({"run_id": run_id, "observation_step": step, "feedback_mode": options["feedback_mode"]}), flush=True)
        def get_action(self):
            step = self.obs["astra_meta"]["step"]
            if self.decisions >= 1 or step >= 5:
                raise RuntimeError("bounded transport smoke reached its one-chunk limit")
            self.decisions += 1
            state, controls = self.obs["state"], self.obs.get("action", {})
            command = {}
            for side in ("left", "right"):
                command[f"{side}_ee_pose"] = np.asarray(state[f"{side}_ee_pose"], dtype=np.float32).copy()
                grip_key = f"{side}_ee_joint_state"
                current_grip = controls.get(grip_key, state.get(grip_key))
                if current_grip is None:
                    raise ValueError("hold smoke requires the current gripper state")
                command[grip_key] = np.asarray(current_grip, dtype=np.float32).reshape(1).copy()
            commands = [{key: value.copy() for key, value in command.items()} for _ in range(5)]
            return pack_actions(commands, base_step=step) if options["action_format"] != "legacy" else commands
        def notify_action_rejection(self, obs):
            raise RuntimeError("hold target was rejected; inspect server control log")
        def finalize(self): pass
    model = HoldModel()
    server = PolicyServer(model)
    ws_url = "ws://127.0.0.1:" + gateway_env.get("ROBOICL_POLICY_GATEWAY_PORT", "8765") + "/external/" + session["policy_session"]
    run_id = None
    started = time.monotonic()
    try:
        async with connect(ws_url, additional_headers={"Authorization": "Bearer " + session["external_policy_token"]},
                           compression=None, max_size=32*1024*1024, open_timeout=20) as websocket:
            run = await asyncio.to_thread(request, "/v1/runs", body | {"policy_session": session["policy_session"]})
            run_id = run["run_id"]
            print(json.dumps({"run_id": run_id, "selected": options}), flush=True)
            async with asyncio.timeout(240):
                async for raw in websocket:
                    frame = decode_envelope(raw)
                    response = await server.process_frame(frame)
                    encoded = encode_frame(response)
                    messages.append({"method": frame.payload.get("func_name", frame.message_type.value),
                                     "received_bytes": len(raw), "sent_bytes": len(encoded),
                                     "formal_step": model.obs["astra_meta"]["step"] if model.obs else None})
                    await websocket.send(encoded)
                    if model.obs is not None and model.obs["astra_meta"]["step"] >= 5:
                        break
    finally:
        if run_id:
            await asyncio.to_thread(request, f"/v1/runs/{run_id}/cancel", {})
            async with asyncio.timeout(30):
                while True:
                    state = await asyncio.to_thread(request, f"/v1/runs/{run_id}")
                    if state["status"] in {"cancelled", "failed", "succeeded"} and state["cleanup_complete"]:
                        break
                    await asyncio.sleep(1)
    diagnostics = await asyncio.to_thread(request, f"/v1/runs/{run_id}/diagnostics?after_seq=0&limit=1000")
    result = {"run_id": run_id, "gpu": state["gpu"], "options": options, "status": state["status"],
              "cleanup_complete": state["cleanup_complete"], "completed_steps": state["completed_steps"],
              "task_success": None, "observations": updates, "messages": messages,
              "sent_application_bytes": sum(item["sent_bytes"] for item in messages),
              "received_application_bytes": sum(item["received_bytes"] for item in messages),
              "elapsed_seconds": time.monotonic()-started, "diagnostics_summary": diagnostics["summary"],
              "route": "loopback HTTP and authenticated gateway WebSocket; public proxy not measured"}
    assert state["completed_steps"] == 5, result
    assert len(updates) == (2 if options["feedback_mode"] == "chunk_boundary" else 6), result
    if options["feedback_mode"] == "chunk_boundary":
        assert updates[-1]["chunk_receipt"]["executed_steps"] == 5
        assert updates[-1]["chunk_receipt"]["discarded_commands"] == 0
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", choices=["baseline", "compact", "chunk", "jpeg"],
                        default=["baseline", "compact", "chunk", "jpeg"])
    parser.add_argument("--output", type=Path, default=ROOT / "results/lean-transport-smoke.json")
    args = parser.parse_args()
    env, gateway = service_env("roboicl-api"), service_env("roboicl-policy-gateway")
    assert urlsplit(env["ROBOICL_POLICY_WSS_URL"]).hostname in {"localhost", "127.0.0.1", "::1"}, "simulator gateway must be loopback"
    results = []
    output = args.output
    cases = {"baseline": {}, "compact": {"action_format": "ee_targets_f32_v1"},
             "chunk": {"action_format": "ee_targets_f32_v1", "feedback_mode": "chunk_boundary"},
             "jpeg": {"action_format": "ee_targets_f32_v1", "feedback_mode": "chunk_boundary",
                      "observation_encoding": "jpeg", "jpeg_quality": 90}}
    for name in args.cases:
        options = transport_options(cases[name])
        results.append(await exercise(options, env, gateway))
        output.write_text(json.dumps(results, indent=2))
    print(json.dumps({"results_file": str(output), "runs": [r["run_id"] for r in results]}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
