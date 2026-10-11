"""Download original NVIDIA material modules used by the RoboDojo scenes."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
from pathlib import Path
import re
import time
import urllib.error
import urllib.request


BASE = "https://omniverse-content-production.s3.us-west-2.amazonaws.com/Materials/2023_1/Base/"
MATERIALS = ("Plastics/Plastic_ABS.mdl", "Metals/Aluminum_Anodized.mdl", "Metals/Aluminum_Cast.mdl")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/remote-material-cache"))
    parser.add_argument("--proxy", help="optional HTTP proxy used only during cache preparation")
    args = parser.parse_args()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"https": args.proxy} if args.proxy else urllib.request.getproxies()))
    entries = []
    pending = list(MATERIALS)
    downloaded = set()
    while pending:
        relative = pending.pop(0)
        if relative in downloaded:
            continue
        started = time.monotonic()
        for attempt in range(3):
            try:
                with opener.open(BASE + relative, timeout=15) as response:
                    data = response.read(32 * 1024 * 1024 + 1)
                break
            except (OSError, urllib.error.URLError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError(f"material dependency exceeds 32 MiB: {relative}")
        if relative.endswith(".png") and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"invalid PNG response: {relative}")
        if relative.endswith(".mdl"):
            if not data.lstrip().startswith(b"mdl "):
                raise ValueError(f"invalid MDL response: {relative}")
            # Preserve all active relative texture dependencies; commented out
            # texture examples are not resources the material actually uses.
            code = re.sub(r"/\*.*?\*/|//[^\n]*", "", data.decode(), flags=re.S)
            for texture in re.findall(r'texture_2d\s*\(\s*"([^"]+)"', code):
                if ":" in texture or texture.startswith("/"):
                    raise ValueError(f"unsupported material dependency: {texture}")
                dependency = posixpath.normpath(posixpath.join(posixpath.dirname(relative), texture))
                if dependency.startswith("../"):
                    raise ValueError("material dependency escapes cache root")
                pending.append(dependency)
        target = args.output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        entries.append({"url": BASE + relative, "path": relative,
                        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        downloaded.add(relative)
        print(f"{relative}: {len(data)} bytes, {time.monotonic() - started:.3f}s")
    manifest = args.output / "manifest.json"
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(json.dumps({"schema_version": "1", "files": entries}, indent=2) + "\n")
    temporary.replace(manifest)
    print(f"Cache ready: {manifest}")


if __name__ == "__main__":
    main()
