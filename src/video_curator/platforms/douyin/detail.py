"""Fetch one video's page response, then reuse yt-dlp's Douyin parser."""

from ...config import Runtime
from ...models import Candidate, CollectionError, Platform
from ...providers.browser import page
from .parsing import parse_video
from .session import detail_response_url, embedded_video, visit_home, wait_for
from .share import fetch as fetch_share
from .urls import parse


def record_from_page(active, candidate: Candidate, runtime: Runtime) -> tuple[dict, str]:
    identity = parse(candidate.url, {})
    if candidate.platform != Platform.DOUYIN or identity is None:
        raise CollectionError("unresolved_identity", "metadata")
    content_id = identity[0]
    records = []

    def capture(response):
        if not detail_response_url(response.url, content_id) or response.status != 200:
            return
        try:
            data = response.json()
        except Exception:
            return
        record = data.get("aweme_detail") if isinstance(data, dict) else None
        if isinstance(record, dict) and str(record.get("aweme_id")) == content_id:
            records.append(record)

    active.on("response", capture)
    visit_home(active, runtime, "metadata")
    active.goto(identity[2], wait_until="domcontentloaded")
    wait_for(active, lambda: bool(records), runtime, "metadata")
    record = records[-1] if records else embedded_video(active, content_id)
    if not record:
        raise CollectionError("guest_detail_unavailable", "metadata")
    return record, content_id


def detail(candidate: Candidate, runtime: Runtime) -> dict:
    identity = parse(candidate.url, {})
    if candidate.platform != Platform.DOUYIN or identity is None:
        raise CollectionError("unresolved_identity", "metadata")
    try:
        info, _ = fetch_share(identity[0], runtime)
        return info
    except CollectionError:
        # One normal desktop-page fallback; CAPTCHA/login gates are never solved.
        pass
    with page(runtime.platforms[Platform.DOUYIN], runtime, stage="metadata") as active:
        record, content_id = record_from_page(active, candidate, runtime)
        return parse_video(record, content_id)
