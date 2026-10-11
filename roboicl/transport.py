"""Explicit, session-scoped policy transport options (stdlib only)."""

TRANSPORT_DEFAULTS = {
    "action_format": "legacy",
    "feedback_mode": "per_step",
    "observation_encoding": "raw_rgb",
    "jpeg_quality": None,
}


def transport_options(values):
    result = {key: values.get(key, default) for key, default in TRANSPORT_DEFAULTS.items()}
    choices = {"action_format": ("legacy", "ee_targets_f32_v1"),
               "feedback_mode": ("per_step", "chunk_boundary"),
               "observation_encoding": ("raw_rgb", "jpeg")}
    for key, allowed in choices.items():
        if result[key] not in allowed:
            raise ValueError(f"{key} must be one of {', '.join(allowed)}")
    quality = result["jpeg_quality"]
    if result["observation_encoding"] == "jpeg":
        if quality is None:
            quality = 90
        if type(quality) is not int or not 1 <= quality <= 100:
            raise ValueError("jpeg_quality must be an integer in [1,100]")
        result["jpeg_quality"] = quality
    elif quality is not None:
        raise ValueError("jpeg_quality requires observation_encoding=jpeg")
    return result


def transport_capabilities():
    return {"schema_version": "1", "action_formats": ["legacy", "ee_targets_f32_v1"],
            "feedback_modes": ["per_step", "chunk_boundary"],
            "observation_encodings": ["raw_rgb", "jpeg"],
            "jpeg_quality": {"default": 90, "min": 1, "max": 100},
            "defaults": TRANSPORT_DEFAULTS.copy(),
            "negotiation": "POST /v1/policy-sessions; selected fields are echoed and bound to the run",
            "compact_max_steps": 64, "coordinate_semantics": "unchanged absolute EE targets, quaternion wxyz"}
