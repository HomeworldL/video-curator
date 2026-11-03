"""Disposable web video acquisition and persistent contact-sheet evidence."""

import hashlib
import io
import json
import math
import shutil
import subprocess
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageOps

from . import download, media
from .config import PreviewSettings, Runtime
from .identity import file_stem
from .models import CollectionError, DownloadRequest, PreviewEvidence, PreviewFrame, VideoMetadata

VERSION = "paged-sampled-frames-v3"


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def source_hash(details: VideoMetadata) -> str:
    return fingerprint(details.model_dump(mode="json"))


def _cover(details: VideoMetadata, runtime: Runtime) -> tuple[Image.Image | None, str | None]:
    if not details.thumbnails:
        return None, "cover_unavailable"
    # Covers are optional. A failed cover must never stand in for an extracted frame.
    headers = {"User-Agent": "Mozilla/5.0", "Referer": details.access_url}
    for url in details.thumbnails[:3]:
        if not url.startswith(("http://", "https://")):
            continue
        try:
            with httpx.stream(
                "GET", url, headers=headers, timeout=runtime.timeout_seconds, follow_redirects=True
            ) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes(65536):
                    data.extend(chunk)
                    if len(data) > 10 * 1024**2:
                        raise ValueError("cover too large")
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 25_000_000:
                    raise ValueError("cover dimensions too large")
                return ImageOps.exif_transpose(image).convert("RGB"), None
        except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError):
            continue
    return None, "cover_fetch_failed"


def obtain(
    details: VideoMetadata,
    root: Path,
    settings: PreviewSettings,
    runtime: Runtime,
    *,
    local_original: Path | None = None,
) -> list[PreviewEvidence]:
    """Acquire both a cover (if available) and sampled video frames; never archive media."""
    signature = fingerprint(
        {
            "source": source_hash(details),
            "preview": settings.model_dump(
                exclude={"cache_budget_bytes", "delete_temporary_video", "max_bytes"}
            ),
            "implementation": VERSION,
        }
    )
    directory = root / "evidence" / ("preview_" + signature)
    manifest = directory / "preview.json"
    cached = root / ".state" / "cache" / "previews" / file_stem(details.identity.key)
    if manifest.is_file():
        try:
            values = json.loads(manifest.read_text())
            pages = []
            for value in values:
                value["path"] = directory / value["path"]
                evidence = PreviewEvidence.model_validate(value)
                if not evidence.path.is_file() or media.sha256(evidence.path) != evidence.sha256:
                    raise ValueError("invalid cached page")
                pages.append(evidence)
            if pages:
                if settings.delete_temporary_video:
                    shutil.rmtree(cached, ignore_errors=True)
                return pages
        except (OSError, ValueError):
            pass
    directory.mkdir(parents=True, exist_ok=True)
    try:
        if local_original is not None:
            if not local_original.resolve().is_relative_to((root / "raw").resolve()):
                raise CollectionError("preview_original_outside_raw", "preview")
            source = local_original
            source_kind = "local_original"
        else:
            # A preview is never a formal download artifact, even on platforms with only one stream.
            shutil.rmtree(cached, ignore_errors=True)
            size = sum(
                p.stat().st_size for p in (root / ".state" / "cache").rglob("*") if p.is_file()
            )
            if size + settings.max_bytes > settings.cache_budget_bytes:
                raise CollectionError("cache_budget_paused", "preview")
            preview_runtime = runtime.model_copy(deep=True)
            platform = preview_runtime.platforms[details.identity.platform]
            platform.format = "bestvideo/best"
            platform.format_sort = [f"res:{settings.resolution}"]
            source = download.obtain(
                DownloadRequest(metadata=details, directory=cached, max_bytes=settings.max_bytes),
                preview_runtime,
            ).path
            source_kind = "web_video"
        info = media.inspect(source)
        if source_kind == "web_video" and info.bytes > settings.max_bytes:
            raise CollectionError("preview_byte_budget", "preview")
        if details.duration_seconds and info.duration_seconds + 2 < details.duration_seconds * 0.9:
            raise CollectionError("preview_incomplete_video", "preview")
        cover, cover_error = _cover(details, runtime)
        pages = []
        for page, (start, end) in enumerate(
            intervals(info.duration_seconds, settings.segment_seconds), 1
        ):
            pages.append(
                _sheet(
                    source,
                    source_kind,
                    info,
                    directory,
                    settings,
                    cover,
                    cover_error,
                    page,
                    start,
                    end,
                )
            )
        data = []
        for evidence in pages:
            value = evidence.model_dump(mode="json")
            value["path"] = evidence.path.name
            data.append(value)
        temporary_manifest = manifest.with_suffix(".tmp")
        temporary_manifest.write_text(json.dumps(data, ensure_ascii=False))
        temporary_manifest.replace(manifest)
        return pages
    except (subprocess.SubprocessError, OSError, ValueError):
        raise CollectionError("preview_frame_extraction_failed", "preview") from None
    finally:
        for frame in directory.glob("frame_*.png"):
            frame.unlink(missing_ok=True)
        if settings.delete_temporary_video:
            shutil.rmtree(cached, ignore_errors=True)


def intervals(duration: float, segment_seconds: int) -> list[tuple[float, float]]:
    """Cover the whole video, including a short final interval."""
    return [
        (start, min(start + segment_seconds, duration))
        for start in range(0, math.ceil(duration), segment_seconds)
    ]


def _sheet(source, source_kind, info, directory, settings, cover, cover_error, page, start, end):
    tile, label_height = settings.tile_size, 30
    columns = math.ceil(math.sqrt(settings.frame_count + 1))
    rows = math.ceil((settings.frame_count + 1) / columns)
    contact = Image.new("RGB", (tile * columns, (tile + label_height) * rows), "#161616")
    draw = ImageDraw.Draw(contact)

    def paste(image, cell, label):
        x, y = cell % columns * tile, cell // columns * (tile + label_height)
        if image is not None:
            fitted = ImageOps.contain(image, (tile, tile))
            contact.paste(fitted, (x + (tile - fitted.width) // 2, y + (tile - fitted.height) // 2))
        else:
            draw.text((x + 20, y + tile // 2), "NO COVER", fill="white")
        draw.text((x + 8, y + tile + 6), label, fill="white")

    paste(cover, 0, "COVER - not a timestamp" if cover else "COVER - unavailable")
    frames = []
    # Keep chronological order and avoid empty opening/closing frames where possible.
    for number in range(1, settings.frame_count + 1):
        timestamp = start + (end - start) * (
            0.5
            if settings.frame_count == 1
            else 0.02 + (number - 1) * 0.91 / (settings.frame_count - 1)
        )
        # A very short final page can otherwise seek beyond the last decodable
        # frame (e.g. 1.93s in a 2s/10fps clip whose final frame is at 1.9s).
        timestamp = min(timestamp, end - min(0.1, (end - start) / 2))
        frame = directory / f"frame_{number}.png"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                str(timestamp),
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                f"scale={tile}:{tile}:force_original_aspect_ratio=decrease",
                str(frame),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        with Image.open(frame) as image:
            paste(image.convert("RGB"), number, f"P{page} F{number} | {timestamp:.2f}s")
        frame.unlink()
        frames.append(PreviewFrame(number=number, timestamp_seconds=timestamp))
    output = directory / f"contact_{page:04d}.jpg"
    temporary = output.with_suffix(".tmp.jpg")
    contact.save(temporary, quality=90)
    temporary.replace(output)
    return PreviewEvidence(
        page=page,
        start_seconds=start,
        end_seconds=end,
        path=output.resolve(),
        sha256=media.sha256(output),
        source=source_kind,
        duration_seconds=info.duration_seconds,
        width=info.width,
        height=info.height,
        cover_present=cover is not None,
        cover_error=cover_error,
        frames=frames,
    )
