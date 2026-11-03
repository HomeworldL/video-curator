"""YouTube native discovery through yt-dlp's maintained search extractor."""

from ...config import Runtime
from ...identity import identify, platform_for_url
from ...models import Candidate, CollectionError, Platform, SearchBatch, SearchRequest
from ...providers import ytdlp


def search(request: SearchRequest, runtime: Runtime) -> SearchBatch:
    if request.platform != Platform.YOUTUBE or request.mode != "native":
        raise CollectionError("youtube_native_search_request_mismatch", "search")
    if request.page != 1:
        raise CollectionError("native_search_has_limit_not_page_cursor", "search")
    raw = ytdlp.extract(
        f"ytsearch{request.limit}:{request.query}",
        runtime.platforms[Platform.YOUTUBE],
        runtime,
        flat=True,
    )
    return _parse(raw, request)


def _parse(raw: dict, request: SearchRequest) -> SearchBatch:
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise CollectionError("native_search_invalid_response", "search")
    candidates = []
    for position, entry in enumerate(entries[: request.limit], 1):
        if not isinstance(entry, dict):
            continue
        url = entry.get("webpage_url") or entry.get("url")
        if (not isinstance(url, str) or not url.startswith("http")) and entry.get("id"):
            url = f"https://www.youtube.com/watch?v={entry['id']}"
        if not isinstance(url, str):
            continue
        try:
            if platform_for_url(url) != Platform.YOUTUBE:
                continue
            identity = identify(url)
        except CollectionError:
            continue
        if identity.provisional:
            continue
        candidates.append(
            Candidate(
                platform=Platform.YOUTUBE,
                url=url,
                title=entry.get("title") if isinstance(entry.get("title"), str) else None,
                author=entry.get("uploader") if isinstance(entry.get("uploader"), str) else None,
                query=request.query,
                mode="native",
                position=str(position),
            )
        )
    if entries and not candidates:
        raise CollectionError("native_search_no_video_entries", "search")
    return SearchBatch(
        request=request,
        candidates=candidates,
        pagination="limit",
        next_page=None,
        stop_reason="limit_reached" if len(entries) >= request.limit else "returned_batch_end",
    )
