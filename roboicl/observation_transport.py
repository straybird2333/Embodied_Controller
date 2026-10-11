"""Encode only outgoing color images; keep local observations as RGB arrays."""

import time

from roboicl.diagnostics import emit


def encode_observation(observation, options):
    if options["observation_encoding"] == "raw_rgb":
        return observation
    from XPolicyLab.utils.process_data import encode_image_bit

    started = time.monotonic_ns()
    result = dict(observation)
    result["vision"] = dict(observation.get("vision", {}))
    raw_bytes = encoded_bytes = 0
    for camera, original in result["vision"].items():
        if not isinstance(original, dict) or "color" not in original:
            continue
        camera_data = dict(original)
        image = camera_data["color"]
        raw_bytes += int(getattr(image, "nbytes", 0))
        camera_data["color"] = encode_image_bit(image, quality=options["jpeg_quality"])
        encoded_bytes += len(camera_data["color"])
        result["vision"][camera] = camera_data
    emit("instant", "observation_image_encode", source="simulator",
         step=observation.get("astra_meta", {}).get("step"),
         attributes={"image_encoding": "jpeg", "jpeg_quality": options["jpeg_quality"],
                     "raw_pixel_bytes": raw_bytes, "encoded_color_bytes": encoded_bytes,
                     "image_encode_seconds": (time.monotonic_ns() - started) / 1e9})
    return result
