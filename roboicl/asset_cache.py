"""Verified local copies of remote material dependencies, outside frozen assets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from roboicl.diagnostics import emit, span


def install_local_asset_aliases(data_root: Path) -> None:
    """Install exact URL aliases before USD/PhysX starts resolving materials.

    Cache population is an explicit setup operation. A normal run performs only
    small local integrity checks, and never downloads or rewrites frozen assets.
    Missing manifests preserve the original resolver behavior.
    """
    manifest_path = data_root / "remote-material-cache" / "manifest.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != "1":
        raise ValueError("unsupported remote material cache schema")
    import omni.client

    total_bytes = 0
    with span("local_material_cache", source="simulator"):
        for entry in manifest["files"]:
            target = (manifest_path.parent / entry["path"]).resolve()
            if not target.is_relative_to(manifest_path.parent.resolve()):
                raise ValueError("material cache path escapes its root")
            data = target.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError(f"material cache integrity mismatch: {target.name}")
            omni.client.set_alias(entry["url"], target.as_uri())
            # Verify the exact original URL resolves to the original bytes.
            # This also catches incompatible Client Library alias semantics.
            result, _, content = omni.client.read_file(entry["url"])
            if result != omni.client.Result.OK or bytes(content) != data:
                raise RuntimeError(f"material URL alias did not resolve: {target.name}")
            total_bytes += len(data)
        emit("instant", "local_material_cache_ready", source="simulator",
             attributes={"file_count": len(manifest["files"]),
                         "file_bytes": total_bytes, "content_preserved": True,
                         "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest()})
