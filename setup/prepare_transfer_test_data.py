#!/usr/bin/env python3
"""Prepare a 10x, uncompressed transfer-test archive from a captured RGB frame."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile


CODE = Path(__file__).resolve().parents[1]
CAMERAS = ("cam_head", "cam_left_wrist", "cam_right_wrist")
COPIES = 10


def find_capture_dir() -> Path:
    candidates = [p.parent for p in CODE.glob("results/*/capture/cam_head_0.png")]
    candidates = [p for p in candidates if all((p / f"{camera}_0.png").is_file() for camera in CAMERAS)]
    if not candidates:
        raise FileNotFoundError("no capture directory with all three camera PNG files under results/")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir", type=Path,
        help="capture directory containing cam_head_0.png, cam_left_wrist_0.png and cam_right_wrist_0.png",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("/home/lfy/.local/state/roboicl/api/transfer-test"),
    )
    args = parser.parse_args()
    source_dir = (args.source_dir or find_capture_dir()).expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    sources = {camera: source_dir / f"{camera}_0.png" for camera in CAMERAS}
    missing = [str(path) for path in sources.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing camera image(s): " + ", ".join(missing))

    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    copies_dir = output_dir / "copies"
    if copies_dir.exists():
        shutil.rmtree(copies_dir)
    copies_dir.mkdir(mode=0o700)
    archive = output_dir / "benchmark.tar"
    archive.unlink(missing_ok=True)

    copied = []
    for copy_index in range(1, COPIES + 1):
        subdir = copies_dir / f"copy_{copy_index:02d}"
        subdir.mkdir(mode=0o700)
        for camera, source in sources.items():
            target = subdir / source.name
            shutil.copyfile(source, target)
            target.chmod(0o600)
            copied.append(target)

    with tarfile.open(archive, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
        for path in copied:
            bundle.add(path, arcname=path.relative_to(copies_dir))
    archive.chmod(0o600)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    metadata = {
        "format": "uncompressed-tar",
        "copies": COPIES,
        "source_images": [path.name for path in sources.values()],
        "file_count": len(copied),
        "bytes": archive.stat().st_size,
        "sha256": digest,
    }
    metadata_path = output_dir / "benchmark.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    metadata_path.chmod(0o600)
    print(json.dumps({"archive": str(archive), **metadata}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
