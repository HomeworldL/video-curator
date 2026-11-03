"""Douyin native browser search. External search is a separate shared provider."""

from urllib.parse import quote

from ...config import Runtime
from ...models import Candidate, CollectionError, Platform, SearchBatch, SearchRequest
from ...providers.browser import page
from .parsing import video_records
from .session import page_gate, search_response_url, visit_home, wait_for


def search(request: SearchRequest, runtime: Runtime) -> SearchBatch:
    if request.platform != Platform.DOUYIN or request.mode != "native":
        raise CollectionError("invalid_native_search_request", "search")
    if request.page != 1:
        raise CollectionError("native_search_has_limit_not_page_cursor", "search")
    records: dict[str, dict] = {}
    valid_pages = []
    with page(runtime.platforms[Platform.DOUYIN], runtime, stage="search") as active:

        def capture(response):
            if not search_response_url(response.url, request.query) or response.status != 200:
                return
            try:
                data = response.json()
            except Exception:
                return
            if (
                not isinstance(data, dict)
                or data.get("status_code") != 0
                or not isinstance(data.get("data"), list)
            ):
                return
            valid_pages.append(data)
            for record in video_records(data["data"]):
                records.setdefault(str(record["aweme_id"]), record)

        active.on("response", capture)
        visit_home(active, runtime, "search")
        active.goto(
            f"https://www.douyin.com/search/{quote(request.query, safe='')}?type=video",
            wait_until="domcontentloaded",
        )
        wait_for(active, lambda: bool(valid_pages), runtime, "search")
        if not valid_pages:
            raise CollectionError("guest_search_unavailable", "search")
        # We only claim a limit-based, bounded search. A scroll position is not a remote cursor.
        for _ in range(min(20, max(1, (request.limit + 9) // 10))):
            if len(records) >= request.limit or not valid_pages[-1].get("has_more"):
                break
            active.mouse.wheel(0, 1600)
            active.wait_for_timeout(700)
            page_gate(active, "search")
    if not records and "has_more" not in valid_pages[-1]:
        raise CollectionError("guest_search_unavailable", "search")
    if not records and valid_pages[-1].get("has_more"):
        raise CollectionError("empty_search_page_with_more", "search")
    candidates = [
        Candidate(
            platform=Platform.DOUYIN,
            url=f"https://www.douyin.com/video/{ident}",
            title=record.get("desc"),
            author=(record.get("author") or {}).get("nickname"),
            query=request.query,
            mode="native",
            position=str(index),
        )
        for index, (ident, record) in enumerate(records.items(), 1)
        if index <= request.limit
    ]
    stop = (
        "limit_reached"
        if len(candidates) >= request.limit
        else ("returned_batch_end" if not valid_pages[-1].get("has_more") else "scroll_budget")
    )
    return SearchBatch(request=request, candidates=candidates, pagination="limit", stop_reason=stop)
