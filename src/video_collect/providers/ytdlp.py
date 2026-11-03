"""Thin yt-dlp wrapper shared by all platforms. No topic or database logic."""

from copy import deepcopy
from http.cookiejar import Cookie
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ..config import PlatformSettings, Runtime
from ..models import CollectionError


class QuietLogger:
    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def options(settings: PlatformSettings, runtime: Runtime) -> dict:
    if settings.cookie_file and not settings.cookie_file.is_file():
        raise CollectionError("cookie_file_missing", "session")
    result = {
        "quiet": True,
        "no_warnings": True,
        "logger": QuietLogger(),
        "socket_timeout": runtime.timeout_seconds,
        "retries": runtime.retries,
        "extractor_retries": runtime.retries,
        "sleep_interval_requests": settings.request_interval_seconds,
        "fragment_retries": runtime.retries,
        "noplaylist": True,
        "cachedir": False,
        "ignoreerrors": False,
        "format": settings.format,
        "skip_unavailable_fragments": False,
    }
    if settings.format_sort:
        result["format_sort"] = settings.format_sort
    if settings.cookie_file:
        result["cookiefile"] = str(settings.cookie_file)
    return result


def _failure(exc: Exception, stage: str) -> CollectionError:
    # Categorize internally; do not leak cookies, signed URLs or upstream payloads.
    message = str(exc).casefold()
    if "fresh cookies" in message:
        code = "needs_fresh_cookies"
    elif any(word in message for word in ("sign in", "login", "log in")):
        code = "needs_login"
    elif "cookie" in message:
        code = "session_cookie_error"
    elif any(word in message for word in ("429", "rate limit", "too many requests")):
        code = "rate_limited"
    elif any(word in message for word in ("403", "412", "precondition failed")):
        code = "platform_access_blocked"
    elif any(word in message for word in ("timed out", "timeout")):
        code = "timeout"
    elif "unsupported url" in message:
        code = "unsupported_url"
    elif "no video formats found" in message:
        code = "media_formats_unavailable"
    else:
        code = "upstream_error"
    return CollectionError(code, stage)


def extract(url: str, settings: PlatformSettings, runtime: Runtime, *, flat=False) -> dict:
    opts = options(settings, runtime)
    if flat:
        opts.update(extract_flat=True, noplaylist=False)
    try:
        with YoutubeDL(opts) as client:
            info = client.extract_info(url, download=False)
            if not info:
                raise CollectionError("empty_response", "metadata")
            return client.sanitize_info(info)
    except DownloadError as exc:
        raise _failure(exc, "search" if flat else "metadata") from None


def download(
    url: str, directory: Path, settings: PlatformSettings, runtime: Runtime, max_bytes: int
) -> Path:
    return _download(directory, settings, runtime, max_bytes, url=url)


def download_info(
    raw_info: dict,
    directory: Path,
    settings: PlatformSettings,
    runtime: Runtime,
    max_bytes: int,
    cookies: list[dict] | None = None,
) -> Path:
    """Download already extracted formats without re-requesting an unsigned detail API."""
    if not raw_info.get("formats") and not raw_info.get("url"):
        raise CollectionError("media_formats_missing", "download")
    return _download(directory, settings, runtime, max_bytes, raw_info=raw_info, cookies=cookies)


def _download(
    directory: Path,
    settings: PlatformSettings,
    runtime: Runtime,
    max_bytes: int,
    *,
    url: str | None = None,
    raw_info: dict | None = None,
    cookies: list[dict] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    budget_exceeded = False

    def check_size(progress):
        nonlocal budget_exceeded
        # Covers separate audio/video tracks and temporary fragments together.
        size = 0
        for path in directory.iterdir():
            try:
                if path.is_file():
                    size += path.stat().st_size
            except FileNotFoundError:
                pass
        estimate = progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0
        if size > max_bytes or estimate > max_bytes:
            budget_exceeded = True
            raise CollectionError("download_byte_budget", "download")

    opts = options(settings, runtime)
    opts.update(
        outtmpl=str(directory / "source.%(ext)s"),
        progress_hooks=[check_size],
        max_filesize=max_bytes,
        merge_output_format="mkv",
        continuedl=True,
        overwrites=False,
    )
    try:
        with YoutubeDL(opts) as client:
            for item in cookies or []:
                expiry = item.get("expires", -1)
                client.cookiejar.set_cookie(
                    Cookie(
                        version=0,
                        name=item["name"],
                        value=item["value"],
                        port=None,
                        port_specified=False,
                        domain=item["domain"],
                        domain_specified=True,
                        domain_initial_dot=item["domain"].startswith("."),
                        path=item.get("path", "/"),
                        path_specified=True,
                        secure=item.get("secure", False),
                        expires=int(expiry) if expiry > 0 else None,
                        discard=expiry <= 0,
                        comment=None,
                        comment_url=None,
                        rest={"HttpOnly": item.get("httpOnly", False)},
                        rfc2109=False,
                    )
                )
            if raw_info is not None:
                info = client.process_ie_result(deepcopy(raw_info), download=True)
            else:
                info = client.extract_info(url, download=True)
            if not info:
                raise CollectionError("empty_download", "download")
    except DownloadError as exc:
        if budget_exceeded:
            raise CollectionError("download_byte_budget", "download") from None
        raise _failure(exc, "download") from None
    files = [
        p
        for p in directory.glob("source.*")
        if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi")
        and ".f" not in p.stem
    ]
    if len(files) != 1:
        sizes = [info.get("filesize") or info.get("filesize_approx") or 0]
        sizes.extend(
            item.get("filesize") or item.get("filesize_approx") or 0
            for item in info.get("requested_formats") or []
        )
        if any(size > max_bytes for size in sizes):
            raise CollectionError("download_byte_budget", "download")
        raise CollectionError("download_output_missing_or_ambiguous", "download")
    if files[0].stat().st_size > max_bytes:
        raise CollectionError("download_byte_budget", "download")
    return files[0]
