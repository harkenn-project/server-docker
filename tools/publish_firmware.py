"""Publish a firmware release to the public firmware repository (see app/updates.py).

    python tools/publish_firmware.py <path to kotodama-firmware clone> [firmware.bin] [--push] [--trailer=TEXT]

Copies the built image to v<version>/firmware.bin in the clone, points latest.json at it and commits. The version
and board come from the image itself (its KOTODAMA-FW marker). With --push, also pushes the commit.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.firmware import inspect  # noqa: E402
from app.updates import is_newer  # noqa: E402

DEFAULT_IMAGE = Path(__file__).resolve().parents[1] / "firmware/.pio/build/kotodama-speaker/firmware.bin"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    repo = Path(args[0])
    image = Path(args[1]) if len(args) > 1 else DEFAULT_IMAGE
    data = image.read_bytes()
    info = inspect(data)
    version = info["version"]

    manifest_path = repo / "latest.json"
    if manifest_path.exists():
        current = json.loads(manifest_path.read_text())["version"]
        if not is_newer(version, current):
            sys.exit(f"{image} is {version}, not newer than the published {current}: bump FW_VERSION first")

    target = repo / f"v{version}" / "firmware.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    manifest = {"version": version, "hardware": info["hardware"], "file": f"v{version}/firmware.bin",
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    git(repo, "add", "latest.json", str(target.relative_to(repo)))
    trailers = [f"--trailer={a[10:]}" for a in sys.argv[1:] if a.startswith("--trailer=")]
    git(repo, "commit", "-q", "-m", f"Firmware {version}", *trailers)
    print(f"committed firmware {version} ({len(data)} bytes, {info['hardware']})")
    if "--push" in sys.argv:
        git(repo, "push", "-q", "origin", "HEAD")
        print("pushed")


if __name__ == "__main__":
    main()
