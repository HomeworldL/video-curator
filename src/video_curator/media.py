"""Local media verification, fixed sampling and original-file archival."""

import hashlib
import json
import math
import subprocess
from pathlib import Path

from .models import CollectionError, MediaInfo


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect(path: Path) -> MediaInfo:
    try:
        response = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            timeout=60,
            check=True,
        )
        info = json.loads(response.stdout)
        video = next(
            s
            for s in info["streams"]
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        )
        duration = float(info["format"].get("duration") or video["duration"])
        width, height = int(video["width"]), int(video["height"])
        if not math.isfinite(duration) or duration <= 0 or min(width, height) < 1:
            raise ValueError("invalid media properties")
        # Decode first and last frames to catch incomplete downloads, not just a readable header.
        for seek in (0, max(0, duration - 0.25)):
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-ss",
                    str(seek),
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                timeout=60,
                check=True,
            )
        return MediaInfo(
            path=path.resolve(),
            bytes=path.stat().st_size,
            sha256=sha256(path),
            duration_seconds=duration,
            width=width,
            height=height,
            has_audio=any(s.get("codec_type") == "audio" for s in info["streams"]),
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration):
        raise CollectionError("invalid_or_unreadable_media", "media") from None


def archive(info: MediaInfo, target: Path) -> MediaInfo:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = inspect(target)
        if existing.sha256 != info.sha256:
            raise CollectionError("archive_conflict", "media")
        return existing
    # Same topic cache/raw filesystem. Hard-link first to avoid overwriting an existing original.
    try:
        target.hardlink_to(info.path)
        info.path.unlink()
    except OSError:
        raise CollectionError("archive_move_failed", "media") from None
    return info.model_copy(update={"path": target.resolve()})
