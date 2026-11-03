"""Select eligible resources, download, verify and archive; never fetch metadata."""

import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import export, media
from .config import Runtime
from .identity import file_stem
from .models import (
    CollectionError,
    DownloadRequest,
    DownloadResult,
    MediaInfo,
    Platform,
    VideoMetadata,
)
from .providers import ytdlp
from .state import TERMINAL


@dataclass
class DownloadBudget:
    lock: object = field(default_factory=threading.Lock)
    reserved_bytes: int = 0


def obtain(request: DownloadRequest, runtime: Runtime) -> DownloadResult:
    if request.metadata.is_live:
        raise CollectionError("live_stream_not_supported", "download")
    backend = request.metadata.raw.get("_backend")
    if request.metadata.identity.platform == Platform.DOUYIN and backend in (
        "douyin-browser",
        "douyin-share",
    ):
        from .platforms.douyin.download import download

        return download(request, runtime)
    if (
        request.metadata.identity.platform == Platform.XIAOHONGSHU
        and backend == "xiaohongshu-browser"
    ):
        from .platforms.xiaohongshu.download import download

        return download(request, runtime)
    path = ytdlp.download(
        request.metadata.access_url,
        request.directory,
        runtime.platforms[request.metadata.identity.platform],
        runtime,
        request.max_bytes,
    )
    return DownloadResult(path=path, backend="yt-dlp")


def existing_media(job, key: str) -> MediaInfo | None:
    artifact = job.state.artifact(key)
    if artifact:
        previous = MediaInfo.model_validate_json(artifact["media_json"])
        if previous.path.is_file():
            actual = media.inspect(previous.path)
            if actual.sha256 != previous.sha256:
                raise CollectionError("previous_media_changed", "media")
            return actual
        # Crash between file archival and the following SQLite commit.
        target = job.root / "raw" / key.split(":")[0] / (file_stem(key) + previous.path.suffix)
        if target.is_file():
            actual = media.inspect(target)
            if actual.sha256 != previous.sha256:
                raise CollectionError("archive_recovery_hash_mismatch", "media")
            return actual
    directory = job.root / ".state" / "cache" / file_stem(key)
    complete = [
        p
        for p in directory.glob("source.*")
        if p.suffix in (".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi") and ".f" not in p.stem
    ]
    if len(complete) == 1:
        return media.inspect(complete[0])
    return None


@contextmanager
def reserve(job, budget):
    with budget.lock:
        size = 0
        for path in (job.root / ".state" / "cache").rglob("*"):
            try:
                if path.is_file():
                    size += path.stat().st_size
            except FileNotFoundError:
                pass  # Another resource may have just moved from cache to raw.
        if (
            size + budget.reserved_bytes + job.topic.download.max_bytes
            > job.topic.download.cache_budget_bytes
        ):
            raise CollectionError("cache_budget_paused", "download")
        budget.reserved_bytes += job.topic.download.max_bytes
    try:
        yield
    finally:
        with budget.lock:
            budget.reserved_bytes -= job.topic.download.max_bytes


def acquire(job, key: str, budget):
    row = job.state.get(key)
    details = VideoMetadata.model_validate_json(row["metadata_json"])
    exclusion = (
        "unsupported_media_type" if details.media_type != "video" or details.is_live else None
    )
    if exclusion:
        raise CollectionError(exclusion, "download")
    info = existing_media(job, key)
    if info:
        job.report(f"[下载 {key}] 复用已有文件，正在核验与归档…")
    if not info:
        with (
            reserve(job, budget),
            job.platform_slot(details.identity.platform),
        ):
            request = DownloadRequest(
                metadata=details,
                directory=job.root / ".state" / "cache" / file_stem(key),
                max_bytes=job.topic.download.max_bytes,
            )
            result = obtain(request, job.runtime)
        job.report(f"[下载 {key}] 文件已获取，正在核验…")
        info = media.inspect(result.path)
    if (
        info.path.is_relative_to(job.root / ".state" / "cache")
        and info.bytes > job.topic.download.max_bytes
    ):
        raise CollectionError("download_byte_budget", "download")
    if details.duration_seconds and info.duration_seconds + 2 < details.duration_seconds * 0.9:
        raise CollectionError("download_is_shorter_than_source_possible_preview", "media")
    job.state.save_artifact(key, info)
    job.state.update(key, "processing", stage="download")
    return details, info


def _process(job, key: str, budget) -> str:
    started = time.monotonic()
    job.report(f"[下载 {key}] 开始，准备获取视频…")
    job.state.update(key, "processing")
    try:
        details, info = acquire(job, key, budget)
        target = job.root / "raw" / details.identity.platform / (file_stem(key) + info.path.suffix)
        info = media.archive(info, target)
        job.state.save_artifact(key, info)
        job.state.update(key, "downloaded", stage="download")
        job.report(
            f"[下载 {key}] 完成：{info.bytes / 1024**2:.1f} MiB，"
            f"耗时 {time.monotonic() - started:.1f}s"
        )
        return "downloaded"
    except CollectionError as exc:
        job.report(f"[下载 {key}] 未完成：{exc.code}")
        if exc.code == "unsupported_media_type":
            job.state.update(key, "rejected", exc.code, stage="download")
            return "rejected"
        status = "budget_paused" if "budget" in exc.code else "technical_error"
        job.state.update(
            key, status, exc.code, attempt=status == "technical_error", stage="download"
        )
        return status
    except Exception as exc:
        job.report(f"[下载 {key}] 失败：internal_error（{type(exc).__name__}）")
        job.state.update(key, "technical_error", "internal_error", attempt=True, stage="download")
        return "technical_error"


def execute(job, keys: list[str]) -> list[str]:
    budget = DownloadBudget()
    # At most 2*workers tasks are ever submitted. Semaphores independently bound heavy stages.
    results = []
    with ThreadPoolExecutor(max_workers=job.topic.download.workers) as pool:
        size = 2 * job.topic.download.workers
        for start in range(0, len(keys), size):
            futures = [
                pool.submit(_process, job, key, budget) for key in keys[start : start + size]
            ]
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                results.extend(f.result() for f in done)
                if done:
                    job.report(
                        f"[下载进度] 已处理 {len(results)}/{len(keys)}：{dict(Counter(results))}"
                    )
                else:
                    job.report(
                        f"[下载进度] 已处理 {len(results)}/{len(keys)}，"
                        f"仍有 {len(pending)} 条在执行或等待；下载、合并或核验中…"
                    )
            job.report("[下载] 正在保存本批结果并更新 videos.jsonl…")
            export.write_stage(
                job.state,
                job.root,
                "download",
                {"status": "running", "processed": len(results)},
            )
    return results


def run(job, limit: int) -> dict:
    """Archive eligible originals using existing metadata and screening results."""
    if job.topic.download.resolution is not None:
        job.runtime = job.runtime.model_copy(deep=True)
        for settings in job.runtime.platforms.values():
            settings.format = "bestvideo+bestaudio/best"
            settings.format_sort = [f"res:{job.topic.download.resolution}"]
    missing = len(job.state.rows(missing_metadata=True))
    if missing:
        job.report(f"[下载] 跳过 {missing} 条缺少详情的记录；可先运行 metadata 补全。")
    keys = [
        row["key"]
        for row in job.state.rows(
            limit=limit,
            max_attempts=job.topic.download.max_attempts,
            attempt_stage="download",
            require_metadata=True,
            exclude_statuses=tuple(sorted(TERMINAL | {"downloaded"})),
            exclude_filtered=True,
            keep_only=job.topic.download.selection in ("keep", "screened"),
            screened_grades=job.topic.download.grades
            if job.topic.download.selection == "screened"
            else None,
            max_duration_seconds=job.topic.download.max_duration_seconds,
        )
    ]
    job.report(
        f"[下载] 本次待处理 {len(keys)} 条，workers={job.topic.download.workers}，"
        f"目标分辨率={job.topic.download.resolution or '最佳可用'}；已下载记录自动跳过。"
    )
    if not keys:
        job.report("[下载] 当前没有符合条件的待下载记录。")
    results = execute(job, keys)
    job.report("[下载] 正在检查归档文件并保存最终汇总…")
    count = export.validate_downloaded(job.state.downloaded_records(), job.root)
    return export.write_stage(
        job.state,
        job.root,
        "download",
        {
            "processed": len(results),
            "outcomes": dict(Counter(results)),
            "downloaded_records": count,
            "missing_metadata": missing,
            "states": job.state.counts(),
            "dedup_scope": "same_platform_same_resource_only",
        },
    )
