"""Download page-provided formats using the common yt-dlp media lifecycle."""

from ...config import Runtime
from ...models import Candidate, CollectionError, DownloadRequest, DownloadResult, Platform
from ...providers import ytdlp
from ...providers.browser import page
from .detail import record_from_page
from .parsing import parse_video
from .share import fetch as fetch_share


def download(request: DownloadRequest, runtime: Runtime) -> DownloadResult:
    if request.metadata.identity.platform != Platform.DOUYIN:
        raise CollectionError("platform_mismatch", "download")
    settings = runtime.platforms[Platform.DOUYIN]
    if request.metadata.raw.get("_backend") == "douyin-share":
        raw, cookies = fetch_share(request.metadata.identity.content_id, runtime)
        path = ytdlp.download_info(
            raw, request.directory, settings, runtime, request.max_bytes, cookies=cookies
        )
        return DownloadResult(path=path, backend="douyin-share+yt-dlp")
    candidate = Candidate(platform=Platform.DOUYIN, url=request.metadata.access_url)
    with page(settings, runtime, stage="download") as active:
        record, content_id = record_from_page(active, candidate, runtime)
        if content_id != request.metadata.identity.content_id:
            raise CollectionError("resolved_resource_mismatch", "download")
        raw = parse_video(record, content_id)
        cookies = active.context.cookies()
        # Cookies remain in memory and belong to this fresh/explicit context only.
        path = ytdlp.download_info(
            raw, request.directory, settings, runtime, request.max_bytes, cookies=cookies
        )
    return DownloadResult(path=path, backend="douyin-browser+yt-dlp")
