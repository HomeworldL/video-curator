"""Fetch source details and normalize common fields exactly once."""

import hashlib
import json
import math
import time
from datetime import UTC, datetime

import httpx

from . import export
from .config import Runtime
from .identity import identify, platform_for_url
from .models import Candidate, CollectionError, Platform, VideoMetadata
from .platforms.bilibili.parts import entry_urls, has_explicit_part
from .platforms.bilibili.urls import access_url as bilibili_access_url
from .providers import ytdlp
from .state import TERMINAL


def _published(raw: dict) -> str | None:
    try:
        if raw.get("timestamp") is not None:
            return datetime.fromtimestamp(float(raw["timestamp"]), UTC).isoformat()
        if raw.get("upload_date"):
            return datetime.strptime(raw["upload_date"], "%Y%m%d").replace(tzinfo=UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _duration(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def normalize(candidate: Candidate, raw: dict) -> VideoMetadata:
    # Do not replace the original signed access URL with an unsigned canonical URL.
    resolved = raw.get("webpage_url") or candidate.url
    if platform_for_url(resolved) != candidate.platform:
        raise CollectionError("resolved_platform_mismatch", "metadata")
    identity = identify(resolved, raw)
    if identity.provisional:
        raise CollectionError("unresolved_identity", "metadata")
    thumbnails = [t["url"] for t in raw.get("thumbnails") or [] if t.get("url")]
    if raw.get("thumbnail") and raw["thumbnail"] not in thumbnails:
        thumbnails.append(raw["thumbnail"])
    access_url = candidate.url if candidate.platform == Platform.XIAOHONGSHU else resolved
    details = VideoMetadata(
        identity=identity,
        access_url=access_url,
        title=raw.get("title"),
        description=raw.get("description"),
        author=raw.get("uploader") or raw.get("channel"),
        author_id=str(raw.get("uploader_id") or raw.get("channel_id") or "") or None,
        published_at=_published(raw),
        duration_seconds=_duration(raw.get("duration")),
        tags=[str(t) for t in raw.get("tags") or []],
        thumbnails=thumbnails,
        media_type="video",
        is_live=bool(raw.get("is_live")),
        raw=raw,
    )

    return compact_common(details)


def fetch(candidate: Candidate, runtime: Runtime) -> VideoMetadata:
    if (
        candidate.platform == Platform.BILIBILI
        and runtime.platforms[candidate.platform].backend == "auto"
    ):
        from .platforms.bilibili.detail import detail

        return normalize(candidate, detail(candidate, runtime))
    url = candidate.url
    if candidate.platform == Platform.BILIBILI:
        url = bilibili_access_url(url)
    # Only known platform shortlink hosts are resolved, validating every redirect.
    if identify(url).provisional:
        try:
            with httpx.Client(timeout=runtime.timeout_seconds, follow_redirects=False) as client:
                for _ in range(6):
                    response = client.get(url)
                    if not response.is_redirect:
                        break
                    url = str(response.next_request.url)
                    if platform_for_url(url) != candidate.platform:
                        raise CollectionError("shortlink_platform_mismatch", "metadata")
        except httpx.HTTPError:
            raise CollectionError("shortlink_resolution_error", "metadata") from None
    resolved_candidate = candidate.model_copy(update={"url": url})
    settings = runtime.platforms[candidate.platform]
    if settings.backend in ("auto", "browser"):
        if candidate.platform == Platform.DOUYIN:
            from .platforms.douyin.detail import detail

            return normalize(resolved_candidate, detail(resolved_candidate, runtime))
        if candidate.platform == Platform.XIAOHONGSHU:
            from .platforms.xiaohongshu.detail import detail

            return normalize(resolved_candidate, detail(resolved_candidate, runtime))
    if settings.backend == "browser":
        raise CollectionError("browser_backend_not_supported_for_platform", "metadata")
    related = []
    if candidate.platform == Platform.BILIBILI and not has_explicit_part(url):
        raw = ytdlp.extract(url, runtime.platforms[candidate.platform], runtime, flat=True)
        if raw.get("_type") == "playlist":
            urls = entry_urls(raw)
            if not urls:
                raise CollectionError("empty_multipart_post", "metadata")
            url, related = urls[0], urls[1:]
            raw = ytdlp.extract(url, runtime.platforms[candidate.platform], runtime)
    else:
        raw = ytdlp.extract(url, runtime.platforms[candidate.platform], runtime)
    if raw.get("_type") in ("playlist", "multi_video"):
        raise CollectionError("multipart_requires_explicit_part_url", "metadata")
    if related:
        raw["_collection_related_parts"] = related
    return normalize(candidate.model_copy(update={"url": url}), raw)


def compact_common(details: VideoMetadata) -> VideoMetadata:
    """Keep provider extensions, removing only successfully represented values."""
    raw = dict(details.raw)
    equivalent = {
        "id": details.identity.content_id,
        "webpage_url": details.access_url,
        "title": details.title,
        "description": details.description,
        "uploader": details.author,
        "tags": details.tags,
        "is_live": details.is_live,
        "media_type": details.media_type,
    }
    for name, value in equivalent.items():
        if name in raw and raw[name] == value:
            raw.pop(name)
    if raw.get("uploader_id") is not None and str(raw["uploader_id"]) == details.author_id:
        raw.pop("uploader_id")
    if "duration" in raw and details.duration_seconds is not None:
        if _duration(raw["duration"]) == details.duration_seconds:
            raw.pop("duration")
    for name in ("timestamp", "upload_date"):
        if name in raw and details.published_at is not None:
            if _published({name: raw[name]}) == details.published_at:
                raw.pop(name)
    if raw.get("thumbnail") in details.thumbnails:
        raw.pop("thumbnail")
    thumbnails = raw.get("thumbnails")
    if isinstance(thumbnails, list) and all(
        isinstance(t, dict) and set(t) == {"url"} and t["url"] in details.thumbnails
        for t in thumbnails
    ):
        raw.pop("thumbnails")
    # channel identifiers, thumbnail dimensions, formats, signed URLs and
    # unknown provider fields are not necessarily duplicated by common fields.
    return details.model_copy(update={"raw": raw})


def parts_reference(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "parts:" + hashlib.sha256(encoded.encode()).hexdigest()


def compact_storage(details: VideoMetadata) -> tuple[VideoMetadata, dict]:
    """Extract versioned Bilibili part lists for atomic storage with the resource."""
    details = compact_common(details)
    raw = dict(details.raw)
    shared = {}
    pages = raw.get("pages")
    if details.identity.platform == Platform.BILIBILI and isinstance(pages, list) and pages:
        matches = [
            p
            for p in pages
            if isinstance(p, dict) and str(p.get("page")) == details.identity.part_id
        ]
        if len(matches) != 1:
            raise CollectionError("invalid_multipart_details", "metadata")
        value = {
            "platform": str(details.identity.platform),
            "content_id": details.identity.content_id,
            "pages": pages,
        }
        reference = parts_reference(value)
        shared[reference] = value
        raw.pop("pages")
        raw.update(part=matches[0], part_count=len(pages), parts_ref=reference)
        if "cid" in raw and raw["cid"] == matches[0].get("cid"):
            raw.pop("cid")
    return details.model_copy(update={"raw": raw}), shared


def validate_parts(details: VideoMetadata, shared: dict):
    reference = details.raw.get("parts_ref")
    if reference is None:
        return
    value = shared.get(reference)
    if not isinstance(value, dict) or parts_reference(value) != reference:
        raise CollectionError("shared_parts_missing_or_invalid", "metadata")
    pages = value.get("pages")
    if (
        value.get("platform") != details.identity.platform
        or value.get("content_id") != details.identity.content_id
        or not isinstance(pages, list)
        or details.raw.get("part_count") != len(pages)
    ):
        raise CollectionError("shared_parts_identity_mismatch", "metadata")
    matches = [
        p for p in pages if isinstance(p, dict) and str(p.get("page")) == details.identity.part_id
    ]
    if len(matches) != 1 or matches[0] != details.raw.get("part"):
        raise CollectionError("shared_part_mismatch", "metadata")


METADATA_STOP_ERRORS = {
    "rate_limited",
    "platform_access_blocked",
    "verification_required",
    "needs_login",
    "needs_fresh_cookies",
    "session_cookie_error",
    "cookie_file_missing",
}
METADATA_TRANSIENT_ERRORS = {
    "timeout",
    "native_detail_network_error",
    "upstream_unavailable",
    "native_detail_invalid_response",
    "native_detail_api_error",
    "native_detail_http_error",
}


def run(
    job,
    limit: int,
    *,
    exclude_statuses: tuple[str, ...] | None = None,
    missing_only=False,
    exclude_filtered=False,
    keep_only=False,
    screened_grades=None,
    max_duration_seconds=None,
) -> list[str]:
    keys = []
    pending = job.state.rows(
        max_attempts=job.topic.metadata.max_attempts,
        attempt_stage="metadata",
        limit=limit,
        exclude_statuses=exclude_statuses,
        missing_metadata=missing_only,
        exclude_filtered=exclude_filtered,
        keep_only=keep_only,
        screened_grades=screened_grades,
        max_duration_seconds=max_duration_seconds,
    )
    job.report(f"[详情] 本次待处理 {len(pending)} 条；已有详情会复用")
    visited = set()
    failed = 0
    consecutive_failures = 0
    stopped = stopped_key = key = None
    started = time.monotonic()
    try:
        for index, row in enumerate(pending):
            key = row["key"]
            if key in visited or len(visited) >= limit:
                continue
            visited.add(key)
            try:
                row = job.state.get(key)
            except KeyError:
                continue  # Another URL in this batch has already merged this identity.
            if row["metadata_json"]:
                keys.append(key)
                continue
            candidate = Candidate.model_validate_json(row["candidate_json"])
            label = f"[详情 {len(visited)}/{len(pending)}] {key}"
            job.report(f"{label} 正在获取…")
            item_started = time.monotonic()
            try:
                with job.platform_slot(candidate.platform):
                    details = fetch(candidate, job.runtime)
                key = job.state.merge_metadata(key, details)
                added_parts = False
                for url in details.raw.get("_collection_related_parts", []):
                    part = candidate.model_copy(update={"url": url, "mode": "parts"})
                    part_key, inserted = job.state.add(part, identify(url))
                    if inserted and len(pending) < limit:
                        pending.append(job.state.get(part_key))
                        added_parts = True
                if added_parts:
                    pending[index + 1 :] = sorted(
                        pending[index + 1 :], key=lambda item: item["record_id"]
                    )
                if job.state.get(key)["status"] not in TERMINAL | {"downloaded"}:
                    job.state.update(key, "metadata_ready", stage="metadata")
                    keys.append(key)
                consecutive_failures = 0
                job.report(
                    f"{label} 成功，统一身份 {key}；耗时 {time.monotonic() - item_started:.1f}s"
                )
            except CollectionError as exc:
                stop = exc.code in METADATA_STOP_ERRORS
                # A platform-wide block must not exhaust a video's retry budget.
                job.state.update(
                    key, "technical_error", exc.code, attempt=not stop, stage="metadata"
                )
                failed += 1
                consecutive_failures = (
                    consecutive_failures + 1 if exc.code in METADATA_TRANSIENT_ERRORS else 0
                )
                job.report(f"{label} 失败：{exc.code}")
                if stop or consecutive_failures >= 3:
                    stopped = exc.code if stop else "consecutive_metadata_failures"
                    stopped_key = key
                    break
            if len(visited) % 50 == 0:
                export.write_stage(job.state, job.root, "metadata", {"status": "running"})
    except KeyboardInterrupt:
        stopped, stopped_key = "interrupted", key
    export.write_stage(
        job.state,
        job.root,
        "metadata",
        {
            "ready": len(set(keys)),
            "failed": failed,
            "processed": len(visited),
            "stopped": stopped,
            "stopped_resource": stopped_key,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        },
    )
    if stopped:
        job.report(f"[详情] 已暂停：{stopped}；进度已保存，未继续请求剩余候选")
        raise CollectionError(stopped, "metadata")
    job.report(
        f"[详情] 本轮结束：可继续处理 {len(set(keys))} 条，失败 {failed} 条；已更新 videos.jsonl"
    )
    return list(dict.fromkeys(keys))
