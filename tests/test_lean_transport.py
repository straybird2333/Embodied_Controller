"""Offline protocol tests: no provider, public network or Isaac imports."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from roboicl.paths import configure_imports
configure_imports("policy")
from client_server.ws.protocol.codec import encode_frame, decode_frame
from client_server.ws.protocol.schemas import Frame
from client_server.ws.protocol.messages import MessageType
from client_server.ws.model_server import PolicyServer
from src.eval_client.ik_continuity import IKContinuityError
from roboicl.compact_actions import pack_actions, unpack_actions, ActionFormatError, StaleActionError
from roboicl.observation_transport import encode_observation
from roboicl.transport import transport_options
from roboicl.policy_sessions import mint_ticket, verify_ticket
from roboicl.policy.eval_failure import PolicyServiceError
from roboicl import rollout


def action():
    return {"left_ee_pose": np.array([-.0, .02, .4, 1, 0, 0, 0], np.float32),
            "left_ee_joint_state": np.array([.4], np.float32),
            "right_ee_pose": np.array([.1, .02, .4, 1, 0, 0, 0], np.float32),
            "right_ee_joint_state": np.array([.5], np.float32)}


class CompactTests(unittest.TestCase):
    def test_bitwise_codec_roundtrip_and_smaller_message(self):
        actions = [action() for _ in range(5)]
        compact = pack_actions(actions, base_step=8)
        encoded = encode_frame({"result": compact})
        restored = unpack_actions(decode_frame(encoded)["result"], expected_step=8)
        for before, after in zip(actions, restored):
            for key in before:
                self.assertEqual(before[key].tobytes(), after[key].tobytes())
        self.assertLess(len(encoded), len(encode_frame({"result": actions})))

    def test_invalid_and_stale_rejected(self):
        valid = pack_actions([action()], base_step=8)
        variants = []
        for dtype in (np.float64, np.float16):
            variants.append(dict(valid, targets=valid["targets"].astype(dtype)))
        variants += [dict(valid, targets=np.zeros((0, 16), np.float32)),
                     dict(valid, targets=np.zeros((1, 15), np.float32)),
                     dict(valid, base_step=True), dict(valid, extra=1)]
        for col, value in ((0, np.nan), (1, np.inf), (3, .5), (7, -1), (15, 1.01)):
            altered = deepcopy(valid)
            altered["targets"][0, col] = value
            variants.append(altered)
        for invalid in variants:
            with self.subTest(invalid=list(invalid)):
                with self.assertRaises(ActionFormatError):
                    unpack_actions(invalid, expected_step=8)
        with self.assertRaises(StaleActionError):
            unpack_actions(valid, expected_step=9)
        with self.assertRaises(ActionFormatError):
            unpack_actions(pack_actions([action()] * 6, base_step=8), expected_step=8, max_steps=5)

    def test_pack_does_not_cast_or_drop_fields(self):
        invalid = action()
        invalid["left_ee_pose"] = invalid["left_ee_pose"].astype(np.float64)
        with self.assertRaises(ActionFormatError):
            pack_actions([invalid], base_step=0)
        with self.assertRaises(ActionFormatError):
            pack_actions([dict(action(), extra=np.array([1], np.float32))], base_step=0)

    def test_jpeg_shared_decoder_rgb_and_no_mutation(self):
        from XPolicyLab.utils.process_data import decode_image_bit
        image = np.zeros((48, 64, 3), np.uint8)
        image[:] = [230, 40, 10]
        depth = np.ones((48, 64), np.float32)
        camera = {"color": image, "depth": depth, "intrinsics": np.eye(3)}
        obs = {"state": action(), "vision": {str(i): camera for i in range(3)}}
        outgoing = encode_observation(obs, transport_options({"observation_encoding": "jpeg"}))
        for value in outgoing["vision"].values():
            decoded = decode_image_bit(value["color"])
            self.assertEqual(decoded.shape, image.shape)
            self.assertLess(np.abs(decoded.astype(float) - image).mean(), 3)
            self.assertIs(value["depth"], depth)
        self.assertIs(outgoing["state"], obs["state"])
        self.assertIs(obs["vision"]["0"]["color"], image)
        self.assertIs(encode_observation(obs, transport_options({})), obs)

    def test_signed_ticket_options_and_old_ticket(self):
        claims = {"sid": "1" * 32, "role": "external_policy", "task": "deposit_coin",
                  "shots": 0, "seed": 0, "layout": 0, "profile": "roboicl-0shot-b25-v1",
                  "reasoning_effort": None, "iat": 100, "exp": 1000}
        secret = b"offline-test-secret" * 3
        self.assertEqual(verify_ticket(secret, mint_ticket(secret, claims), now=101), claims)
        scoped = claims | transport_options({"action_format": "ee_targets_f32_v1"})
        self.assertEqual(verify_ticket(secret, mint_ticket(secret, scoped), now=101), scoped)
        with self.assertRaises(ValueError):
            verify_ticket(secret, mint_ticket(secret, claims | {"feedback_mode": "chunk_boundary"}), now=101)
        with self.assertRaises(ValueError):
            verify_ticket(secret, mint_ticket(secret, scoped | {"jpeg_quality": 90}), now=101)

    def test_duplicate_rpc_caches_compact_result(self):
        class Model:
            calls = 0
            def get_action(self):
                self.calls += 1
                return pack_actions([action()], base_step=0)
        async def check():
            model = Model()
            server = PolicyServer(model)
            frame = Frame(message_type=MessageType.CALL, request_id="same-request",
                          evaluation_id="offline", payload={"func_name": "get_action"})
            first = await server.process_frame(frame)
            second = await server.process_frame(frame)
            self.assertEqual(model.calls, 1)
            self.assertEqual(encode_frame(first), encode_frame(second))
        asyncio.run(check())


class RolloutTests(unittest.TestCase):
    def exercise(self, options, *, reject=None, end_at=5, tracking=False, stale=False):
        updates, captured, attempted = [], [], []
        env = SimpleNamespace(num_envs=1, save_dir=tempfile.gettempdir(), step_lim=100,
                              completed=0, done=False)
        def take_action(command):
            attempted.append(command)
            if reject is not None and len(attempted) - 1 == reject:
                env.done = True
                raise IKContinuityError([{"passed": False, "arm": "right_arm_joint_state",
                                          "error": "joint_target_jump", "max_current_delta_rad": .3,
                                          "limit_rad": .15}])
            env.completed += 1
        env.take_action = take_action
        env.is_episode_end = lambda: env.done or env.completed >= end_at
        def capture(_env, *, step=0):
            captured.append(step)
            return {"state": action(), "vision": {}}
        class Client:
            def call(self, *, func_name, **kwargs):
                return getattr(self, func_name)(**kwargs)
            def reset(self): pass
            def update_obs(self, obs): updates.append(deepcopy(obs))
            def get_action(self):
                commands = [action() for _ in range(5)]
                if options.get("action_format") == "ee_targets_f32_v1":
                    return pack_actions(commands, base_step=1 if stale else env.completed)
                return commands
            def notify_action_rejection(self, obs): pass
            def finalize(self): env.finalized = True
        config = SimpleNamespace(ik_continuity_guard=True, max_joint_increment_rad=.15,
                                 max_turns=2, include_head_calibration=False, execute_horizon=5)
        feedback = {"left": {"position_error_m": .2}} if tracking else {}
        with patch.dict(os.environ, {"ROBOICL_TRANSPORT_OPTIONS": json.dumps(options),
                                     "ASTRA_TRACKING_GUARD": "1"}), \
             patch.object(rollout, "load_config", return_value=config), \
             patch.object(rollout, "_ready_observation", side_effect=capture), \
             patch.object(rollout, "tracking_receipt", return_value=feedback):
            try:
                rollout.eval_one_episode(env, Client())
            except PolicyServiceError:
                if not stale: raise
        return env, updates, captured, attempted

    def test_legacy_unchanged_and_boundary_only_sends_two_observations(self):
        legacy = self.exercise({})
        boundary = self.exercise({"action_format": "ee_targets_f32_v1", "feedback_mode": "chunk_boundary"})
        self.assertEqual(len(legacy[1]), 6)
        self.assertEqual(len(boundary[1]), 2)
        self.assertEqual(legacy[2], boundary[2])
        self.assertEqual(boundary[2], [0, 1, 2, 3, 4, 5])
        receipt = boundary[1][-1]["astra_meta"]["chunk_receipt"]
        self.assertEqual((receipt["executed_steps"], receipt["last_step"], receipt["discarded_commands"]), (5, 5, 0))

    def test_partial_and_zero_step_rejection(self):
        for index in (0, 2):
            env, updates, captured, attempted = self.exercise({"action_format": "ee_targets_f32_v1",
                                                               "feedback_mode": "chunk_boundary"}, reject=index)
            receipt = updates[-1]["astra_meta"]["chunk_receipt"]
            self.assertEqual(env.completed, index)
            self.assertEqual(len(attempted), index + 1)
            self.assertEqual(receipt["executed_steps"], index)
            self.assertEqual(receipt["rejection_index"], index)
            self.assertEqual(receipt["discarded_commands"], 5 - index)
            self.assertEqual(captured[-1], index)
            self.assertEqual(receipt["reason"], "ik_rejected")
            self.assertTrue(env.finalized)

    def test_termination_discards_suffix(self):
        _, updates, _, attempted = self.exercise({"feedback_mode": "chunk_boundary"}, end_at=3)
        receipt = updates[-1]["astra_meta"]["chunk_receipt"]
        self.assertEqual(len(attempted), 3)
        self.assertEqual(receipt["discarded_commands"], 2)
        self.assertTrue(receipt["terminated"])

    def test_tracking_interruption_still_checks_each_step(self):
        _, updates, _, attempted = self.exercise({"feedback_mode": "chunk_boundary"}, end_at=4, tracking=True)
        self.assertEqual(len(attempted), 4)
        self.assertEqual(len(updates), 3)
        receipt = updates[1]["astra_meta"]["chunk_receipt"]
        self.assertEqual(receipt["reason"], "tracking_interrupted")
        self.assertEqual(receipt["executed_steps"], 2)
        self.assertEqual(receipt["discarded_commands"], 3)

    def test_stale_chunk_rejected_before_action_dispatch(self):
        env, updates, _, attempted = self.exercise({"action_format": "ee_targets_f32_v1",
                                                   "feedback_mode": "chunk_boundary"}, stale=True)
        self.assertEqual(attempted, [])
        self.assertEqual(env._policy_failure["error_code"], "stale_action_base_step")
        self.assertEqual(updates[-1]["astra_meta"]["chunk_receipt"]["executed_steps"], 0)


class SessionTests(unittest.TestCase):
    def test_inherit_conflict_ownership_reuse_and_db_defaults(self):
        from roboicl.service import RunManager
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"ROBOICL_POLICY_WSS_URL": "wss://offline.invalid/session",
                                     "ROBOICL_POLICY_TICKET_SECRET": "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eA"}):
            manager = RunManager(Path(directory))
            try:
                body = {"task": "deposit_coin", "shots": 0, "seed": 0, "layout": 0,
                        "profile": "roboicl-0shot-b25-v1"}
                session = manager.create_session(body | {"action_format": "ee_targets_f32_v1",
                                                        "feedback_mode": "chunk_boundary",
                                                        "observation_encoding": "jpeg"}, "owner", "offline.invalid")
                run_body = body | {"policy_session": session["policy_session"]}
                with self.assertRaisesRegex(ValueError, "transport_mismatch"):
                    manager.create(run_body | {"feedback_mode": "per_step"}, "owner")
                with self.assertRaises(ValueError):
                    manager.create(run_body, "different-owner")
                run = manager.create(run_body, "owner")
                self.assertEqual(run["action_format"], "ee_targets_f32_v1")
                self.assertEqual(run["jpeg_quality"], 90)
                with self.assertRaises(ValueError):
                    manager.create(run_body, "owner")
                old = manager.create_session(body, "owner", "offline.invalid")
                self.assertEqual(old["action_format"], "legacy")
                self.assertEqual(old["feedback_mode"], "per_step")
                self.assertEqual(old["observation_encoding"], "raw_rgb")
            finally:
                manager.close()

    def test_gateway_mismatched_peer_releases_its_slot(self):
        from roboicl.policy_gateway import PolicyGateway, _RelaySession
        async def check():
            gateway = PolicyGateway.__new__(PolicyGateway)
            sid = "2" * 32
            class Peer:
                async def close(self, *, code, reason):
                    self.code = code
            peer = Peer()
            gateway.connection_claims = {id(peer): {"sid": sid, "role": "external_policy",
                **transport_options({"feedback_mode": "chunk_boundary"})}}
            original = object()
            gateway.relay_sessions = {sid: _RelaySession(simulator=original, transport=transport_options({}))}
            gateway.relay_guard = threading.Lock()
            gateway.guard = threading.Lock()
            gateway.active_sids = {sid}
            gateway.active_external_sids = {sid}
            await gateway.handler(peer)
            self.assertEqual(peer.code, 1008)
            self.assertNotIn(sid, gateway.active_external_sids)
            self.assertIn(sid, gateway.active_sids)
            self.assertIs(gateway.relay_sessions[sid].simulator, original)
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
