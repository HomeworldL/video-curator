"""One search entry point. Calls platform functions only when they are needed."""

import json
import time
from urllib.parse import urlsplit

from . import export, jobs
from .config import Runtime, Topic
from .identity import DOMAINS, identify, platform_for_url
from .models import Candidate, CollectionError, Platform, SearchBatch, SearchRequest
from .platforms.bilibili.search import search as bilibili_search
from .platforms.douyin.search import search as douyin_search
from .platforms.xiaohongshu.search import search as xiaohongshu_search
from .platforms.youtube.search import search as youtube_search
from .providers import web

NATIVE_SEARCH = {
    Platform.YOUTUBE: youtube_search,
    Platform.BILIBILI: bilibili_search,
    Platform.DOUYIN: douyin_search,
    Platform.XIAOHONGSHU: xiaohongshu_search,
}


def plan(topic: Topic, runtime: Runtime, page_size: int | None = None) -> list[SearchRequest]:
    return [
        SearchRequest(
            platform=platform,
            query=query,
            mode=topic.search.mode or settings.search_mode,
            limit=page_size or topic.search.page_size,
        )
        for platform, settings in runtime.platforms.items()
        for query in topic.search.queries_for(platform)
    ]


def discover(request: SearchRequest, runtime: Runtime) -> SearchBatch:
    if request.mode == "native":
        return NATIVE_SEARCH[request.platform](request, runtime)
    query = f"site:{DOMAINS[request.platform][0]} {request.query}"
    results = web.search(query, request.limit, request.page, runtime.timeout_seconds)
    candidates = []
    for position, row in enumerate(results):
        url = row.get("href") or row.get("url")
        if not url:
            continue
        try:
            identity = identify(url)
        except CollectionError:
            continue
        if identity.platform != request.platform:
            continue
        # Avoid treating an author/search page as an unresolved video candidate.
        if identity.provisional:
            parts = urlsplit(url)
            if parts.hostname not in (
                "b23.tv",
                "xhslink.com",
                "v.douyin.com",
            ) or not parts.path.strip("/"):
                continue
        candidates.append(
            Candidate(
                platform=request.platform,
                url=url,
                title=row.get("title"),
                query=request.query,
                mode="web",
                position=f"{request.page}:{position + 1}",
            )
        )
    return SearchBatch(
        request=request,
        candidates=candidates,
        pagination="page",
        next_page=request.page + 1 if len(results) >= request.limit else None,
        stop_reason="page_limit" if len(results) >= request.limit else "returned_batch_end",
    )


def discover_and_save(job, request: SearchRequest) -> dict:
    try:
        with job.platform_slot(request.platform):
            result = discover(request, job.runtime)
    except CollectionError as exc:
        job.state.search_result(request.model_dump(mode="json"), error=exc.code)
        raise
    new = duplicates = 0
    identified = [(candidate, identify(candidate.url)) for candidate in result.candidates]
    for candidate, identity in identified:
        _, inserted = job.state.add(candidate, identity)
        new += inserted
        duplicates += not inserted
    job.state.search_result(request.model_dump(mode="json"), result.model_dump(mode="json"))
    return {
        "platform": request.platform,
        "mode": request.mode,
        "new": new,
        "duplicates": duplicates,
        "returned": len(result.candidates),
        "next_page": result.next_page,
        "pagination": result.pagination,
        "stop_reason": result.stop_reason,
    }


def run(job, requests: list[SearchRequest], *, all_pages=False, max_pages=None):
    """Execute a saved search plan, resuming completed pages and exporting progress."""
    plan = {
        "requests": [r.model_dump(mode="json") for r in requests],
        "all_pages": all_pages,
        "max_pages": max_pages,
    }
    jobs.save_search_plan(job.root, plan)
    total = sum(job.state.counts().values())
    job.report(f"[搜索] 共 {len(requests)} 个查询；已有 {total} 条候选；输出 {job.root}")
    results = []
    stopped = None
    try:
        for query_index, initial in enumerate(requests, 1):
            request = initial
            label = f"[搜索 {query_index}/{len(requests)}] {request.platform} {request.mode} {json.dumps(request.query, ensure_ascii=False)}"
            seen_pages = set()
            result = {
                "platform": request.platform,
                "query": request.query,
                "mode": request.mode,
                "pages": 0,
                "reused_pages": 0,
                "returned_hits": 0,
            }
            results.append(result)
            while True:
                saved = job.state.completed_search(request.model_dump(mode="json"))
                if saved:
                    result["reused_pages"] += 1
                    job.report(
                        f"{label} 第 {request.page} 页：复用已完成结果，{saved['candidate_count']} 条命中"
                    )
                else:
                    job.report(f"{label} 第 {request.page} 页：正在请求…")
                    started = time.monotonic()
                    try:
                        counts = discover_and_save(job, request)
                    except CollectionError as exc:
                        result.update(error=exc.code, stop_reason=exc.code)
                        stopped = exc.code
                        job.report(f"{label} 第 {request.page} 页失败：{exc.code}")
                        break
                    total += counts["new"]
                    job.report(
                        f"{label} 第 {request.page} 页：返回 {counts['returned']}，新增 {counts['new']}，"
                        f"重复 {counts['duplicates']}，累计 {total} 条；耗时 {time.monotonic() - started:.1f}s"
                    )
                    saved = job.state.completed_search(request.model_dump(mode="json"))
                batch = saved
                result["pages"] += 1
                result["returned_hits"] += batch["candidate_count"]
                result["pagination"] = batch["pagination"]
                signature = batch["signature"]
                if signature in seen_pages and batch["candidate_count"]:
                    result["stop_reason"] = "repeated_search_page"
                    stopped = "repeated_search_page"
                    break
                seen_pages.add(signature)
                result["stop_reason"] = batch["stop_reason"]
                export.write_stage(
                    job.state, job.root, "search", {"status": "running", "queries": results}
                )
                if not all_pages:
                    break
                if batch["pagination"] == "limit":
                    result["stop_reason"] = "all_pages_not_supported_for_entry"
                    stopped = result["stop_reason"]
                    break
                if batch["next_page"] is None:
                    break
                if batch["next_page"] <= request.page:
                    result["stop_reason"] = "invalid_next_page"
                    stopped = result["stop_reason"]
                    break
                if max_pages and result["pages"] >= max_pages:
                    result["stop_reason"] = "max_pages_reached"
                    stopped = result["stop_reason"]
                    break
                request = request.model_copy(update={"page": batch["next_page"]})
            export.write_stage(
                job.state, job.root, "search", {"status": "running", "queries": results}
            )
            job.report(
                f"{label} 本词结束：{result['pages']} 页，{result.get('stop_reason', 'unknown')}"
            )
            if result.get("error") in (
                "platform_access_blocked",
                "rate_limited",
                "verification_required",
            ):
                break
    except KeyboardInterrupt:
        stopped = "interrupted"
    summary = export.write_stage(
        job.state,
        job.root,
        "search",
        {
            "status": "partial" if stopped else "complete",
            "scope": "reported_pages" if all_pages else "requested_batches",
            "stopped": stopped,
            "queries_planned": len(requests),
            "queries": results,
        },
    )
    outcome = f"未完成（{stopped}），重跑同一命令可接续" if stopped else "完成"
    job.report(
        f"[搜索] {outcome}；共 {summary['candidate_records']} 条去重候选，已保存 videos.jsonl 和 task.json"
    )
    return summary


def import_urls(job, urls: list[str]) -> dict:
    new = duplicates = 0
    for url in urls:
        candidate = Candidate(url=url, platform=platform_for_url(url))
        _, inserted = job.state.add(candidate, identify(url))
        new += inserted
        duplicates += not inserted
    return {"new": new, "duplicates": duplicates}
