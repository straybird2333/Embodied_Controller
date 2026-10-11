"""Lossless matrix representation of already validated float32 EE targets."""

import numpy as np


FORMAT = "ee_targets_f32_v1"
KEYS = ("left_ee_pose", "left_ee_joint_state", "right_ee_pose", "right_ee_joint_state")


class ActionFormatError(ValueError):
    error_code = "invalid_action_payload"


class StaleActionError(ActionFormatError):
    error_code = "stale_action_base_step"


def unpack_actions(payload, *, expected_step, max_steps=64):
    if not isinstance(payload, dict) or set(payload) != {"format", "base_step", "targets"}:
        raise ActionFormatError("compact actions require format, base_step and targets")
    if payload["format"] != FORMAT:
        raise ActionFormatError("unsupported action format")
    base_step = payload["base_step"]
    if type(base_step) is not int or base_step < 0:
        raise ActionFormatError("base_step must be a nonnegative integer")
    if base_step != expected_step:
        raise StaleActionError("action base_step differs from the current formal observation step")
    targets = payload["targets"]
    if not isinstance(targets, np.ndarray) or targets.dtype != np.dtype("float32"):
        raise ActionFormatError("targets must be a float32 numpy matrix")
    if targets.ndim != 2 or targets.shape[1] != 16 or not 1 <= len(targets) <= min(max_steps, 64):
        raise ActionFormatError(f"targets must have shape [1..{min(max_steps,64)},16]")
    if not np.isfinite(targets).all():
        raise ActionFormatError("targets contain nonfinite values")
    for offset in (0, 8):
        norms = np.linalg.norm(targets[:, offset+3:offset+7], axis=1)
        if np.any(np.abs(norms - 1) > 0.001):
            raise ActionFormatError("target quaternion must be unit length in wxyz order")
        grips = targets[:, offset+7]
        if np.any((grips < 0) | (grips > 1)):
            raise ActionFormatError("gripper targets must be in [0,1]")
    return [{KEYS[0]: row[:7].copy(), KEYS[1]: row[7:8].copy(),
             KEYS[2]: row[8:15].copy(), KEYS[3]: row[15:16].copy()} for row in targets]


def pack_actions(actions, *, base_step):
    if not isinstance(actions, list) or not 1 <= len(actions) <= 64:
        raise ActionFormatError("actions must contain 1..64 targets")
    targets = np.empty((len(actions), 16), dtype=np.float32)
    for index, action in enumerate(actions):
        if not isinstance(action, dict) or set(action) != set(KEYS):
            raise ActionFormatError("compact format only supports the four EE target fields")
        offset = 0
        for key, width in zip(KEYS, (7, 1, 7, 1)):
            value = action[key]
            if not isinstance(value, np.ndarray) or value.dtype != np.dtype("float32") or value.shape != (width,):
                raise ActionFormatError("packing requires existing float32 arrays; no conversion is performed")
            targets[index, offset:offset+width] = value
            offset += width
    result = {"format": FORMAT, "base_step": base_step, "targets": targets}
    unpack_actions(result, expected_step=base_step)
    return result
