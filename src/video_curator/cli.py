"""Unified stage commands; stage modules own scheduling and persistence."""

import argparse
import importlib.util
import json
import math
import os
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from . import download, export, filtering, jobs, metadata, screening, search
from .config import load_runtime, resolve_topic
from .jobs import Job
from .models import CollectionError, Platform


def _progress(message: str):
    print(f"{datetime.now():%H:%M:%S} {message}", file=sys.stderr, flush=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="按主题发现、同站去重、下载和筛选原视频")
    root.add_argument("--topic", help="主题YAML路径或直接搜索的文本；省略则使用Labubu测试配置")
    root.add_argument("--runtime", type=Path, help="路径相对该YAML所在目录解析；省略则使用默认配置")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查依赖、登录文件和已接通的入口，不请求网络")
    status = sub.add_parser("status", help="查看候选状态，不调用VLM")
    status.add_argument("--input", type=Path, help="已有任务目录")
    sub.add_parser("prompt", help="查看主题提示词和输出Schema，不调用VLM")
    discover = sub.add_parser("search", help="只搜索并保存候选，不下载或审核")
    discover.add_argument(
        "--topic",
        default=argparse.SUPPRESS,
        help="主题YAML路径或直接搜索的文本；自动使用其中全部搜索词",
    )
    discover.add_argument("--platform", choices=list(Platform))
    discover.add_argument(
        "--mode",
        choices=["native", "web"],
        help="native=平台站内搜索，web=搜索引擎站外发现；默认使用平台配置",
    )
    discover.add_argument(
        "--page-size", type=int, help="每个搜索请求的条数，默认读取search.page_size"
    )
    discover.add_argument("--page", type=int, default=1)
    discover.add_argument("--output", type=Path, help="创建或恢复任务目录，供下一阶段 --input 使用")
    discover.add_argument(
        "--all-pages",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="沿入口报告的下一页继续",
    )
    discover.add_argument("--max-pages", type=int, help="每个词最多处理的页数")
    imp = sub.add_parser("import-urls", help="导入文本URL或含url字段的JSONL")
    imp.add_argument("input", type=Path)
    details = sub.add_parser("metadata", help="补全详情，不下载完整媒体")
    details.add_argument("--limit", type=int)
    details.add_argument("--input", type=Path, help="读取搜索阶段的任务目录")
    text_filter = sub.add_parser(
        "filter", help="按批次筛选已有标题、简介和标签，不下载、不读取画面"
    )
    text_filter.add_argument("--input", type=Path, required=True, help="已有任务目录")
    text_filter.add_argument(
        "--topic", default=argparse.SUPPRESS, help="读取同主题 YAML 的 filter 配置"
    )
    text_filter.add_argument("--runtime", type=Path, default=argparse.SUPPRESS, help="平台环境配置")
    text_filter.add_argument(
        "--limit", type=int, help="本次最多处理的未审核记录数；省略则全部已有详情"
    )
    text_filter.add_argument(
        "--workers",
        type=int,
        choices=range(1, 9),
        help="同时请求的批次数，覆盖运行配置；默认 1",
    )
    text_filter.add_argument(
        "--retry-failed", action="store_true", help="显式重试失败/结果未知的记录，可能再次计费"
    )
    screen = sub.add_parser("screen", help="文本keep的封面与可配置抽帧预览、VLM分档，不归档原片")
    screen.add_argument("--input", type=Path, required=True)
    screen.add_argument("--topic", default=argparse.SUPPRESS, help="主题YAML的screen配置")
    screen.add_argument("--runtime", type=Path, default=argparse.SUPPRESS)
    screen.add_argument(
        "--limit", type=int, help="本轮最多处理的待审记录；默认全部，受max_calls限制"
    )
    screen.add_argument("--workers", type=int, choices=range(1, 9))
    screen.add_argument("--retry-failed", action="store_true")
    screen.add_argument(
        "--resample-legacy", action="store_true", help="补审超过分页时长的旧版单页成功结果"
    )
    screen.add_argument(
        "--new-version", action="store_true", help="确认提示词、模型或采样变化后重新审核"
    )
    command = sub.add_parser("download", help="下载视觉筛选通过的原片并核验归档，不调用VLM")
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--limit", type=int)
    command.add_argument("--max-duration-seconds", type=float, help="最大视频时长（秒）；默认不限")
    command.add_argument("--resolution", type=int, help="优先清晰度，如720，按短边选择")
    command.add_argument(
        "--selection",
        choices=["screened", "keep", "non-rejected"],
        help="默认要求文本keep且视觉档位允许；keep显式跳过视觉筛选",
    )
    command.add_argument("--workers", type=int, choices=range(1, 17))
    for stage in (details, command):
        stage.add_argument("--topic", default=argparse.SUPPRESS, help="本阶段的主题配置")
    exporter = sub.add_parser("export", help="刷新两个交接文件，或显式导出 CSV／记录子集")
    exporter.add_argument("--input", type=Path, help="已有任务目录")
    exporter.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    exporter.add_argument("--selection", choices=["all", "downloaded", "accepted"], default="all")
    exporter.add_argument("--output", type=Path, help="显式导出文件，不改变任务清单")
    return root


def main(argv=None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    try:
        if getattr(args, "limit", 1) is not None and getattr(args, "limit", 1) < 1:
            cli.error("--limit must be positive")
        if (
            args.command == "search"
            and args.page_size is not None
            and not 1 <= args.page_size <= 200
        ):
            cli.error("--page-size must be between 1 and 200")
        job_input = getattr(args, "input", None) if args.command != "import-urls" else None
        job_output = args.output if args.command == "search" else None
        job_root = job_input or job_output
        task = jobs.read(job_input.resolve()) if job_input else None
        if task is not None:
            topic, runtime = jobs.load(job_input.resolve(), task=task)
            if args.runtime:
                runtime = load_runtime(args.runtime)
            if args.topic and args.command in ("metadata", "filter", "screen", "download"):
                override = resolve_topic(args.topic)
                if override.id != topic.id:
                    raise CollectionError(f"{args.command}_topic_id_mismatch", "config")
                setattr(topic, args.command, getattr(override, args.command))
                if args.command == "screen":
                    topic.download.grades = override.download.grades
        else:
            topic, runtime = resolve_topic(args.topic), load_runtime(args.runtime)
        if args.command in ("filter", "screen") and args.workers is not None:
            getattr(topic, args.command).workers = args.workers
        if getattr(args, "max_pages", None) is not None and args.max_pages < 1:
            cli.error("--max-pages must be positive")
        if job_output:
            jobs.initialize(job_output, topic, runtime)
        if args.command == "download":
            if args.selection is not None:
                topic.download.selection = args.selection
            if args.workers is not None:
                topic.download.workers = args.workers
            if args.max_duration_seconds is not None:
                if not math.isfinite(args.max_duration_seconds) or args.max_duration_seconds <= 0:
                    cli.error("--max-duration-seconds must be a finite positive number of seconds")
                topic.download.max_duration_seconds = args.max_duration_seconds
            if args.resolution is not None:
                if args.resolution <= 0:
                    cli.error("--resolution must be positive")
                topic.download.resolution = args.resolution
        if args.command == "screen":
            _progress(f"[screen] 启动：{job_input.resolve()}")
            output = screening.run(
                job_input.resolve(),
                topic,
                runtime,
                limit=args.limit,
                retry_failed=args.retry_failed,
                new_version=args.new_version,
                resample_legacy=args.resample_legacy,
                progress=_progress,
            )
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 2 if output.get("stopped") or output["status"] == "partial" else 0
        job = Job(
            topic,
            runtime,
            root=job_root.resolve() if job_root else None,
            progress=_progress,
            task=task,
        )
        if args.command == "doctor":
            output = {
                "executables": {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe")},
                "vlm": {
                    "key_configured": bool(os.environ.get(topic.screen.api_key_env)),
                    "model_configured": bool(
                        os.environ.get(topic.screen.model_env) or topic.screen.model
                    ),
                },
                "platforms": {
                    p: {
                        "search_mode": cfg.search_mode,
                        "implemented_modes": ["native", "web"],
                        "cookie_file_exists": bool(cfg.cookie_file and cfg.cookie_file.is_file()),
                        "backend": cfg.backend,
                        "default_download_backend": "platform-specific"
                        if p in (Platform.DOUYIN, Platform.XIAOHONGSHU)
                        else "yt-dlp",
                        "session_source": "explicit_configuration"
                        if cfg.cookie_file or cfg.browser_state
                        else "fresh_guest",
                        "network_checked": False,
                    }
                    for p, cfg in runtime.platforms.items()
                },
                "browser_dependency_installed": importlib.util.find_spec("playwright") is not None,
            }
        elif args.command == "status":
            with job.exclusive():
                rows = job.state.rows()
            output = {
                "states": job.state.counts(),
                "errors": dict(Counter(r["error_code"] for r in rows if r["error_code"])),
                "resources": len(rows),
                "filter_errors": filtering.error_summary(job.state.text_filters()),
                "dataset": str(job.root),
            }
        elif args.command == "prompt":
            from .models import ScreenResult

            if topic.screen.prompt is None:
                raise CollectionError("screen_prompt_required", "config")
            output = {
                "prompt": topic.screen.prompt,
                "output_schema": ScreenResult.model_json_schema(),
            }
        else:
            _progress(f"[{args.command}] 启动，正在检查任务状态：{job.root}")
            with job.exclusive():
                if args.command == "search":
                    search.import_urls(job, topic.search.seeds)
                    requests = search.plan(topic, runtime, args.page_size)
                    if args.platform:
                        if args.platform not in runtime.platforms:
                            raise CollectionError("platform_not_configured", "config")
                        requests = [r for r in requests if r.platform == args.platform]
                    requests = [
                        r.model_copy(update={"mode": args.mode or r.mode, "page": args.page})
                        for r in requests
                    ]
                    if not requests:
                        raise CollectionError("no_search_queries_for_platform", "search")
                    output = search.run(
                        job,
                        requests,
                        all_pages=topic.search.all_pages
                        if args.all_pages is None
                        else args.all_pages,
                        max_pages=args.max_pages or topic.search.max_pages,
                    )
                elif args.command == "import-urls":
                    urls = []
                    for line in args.input.read_text(encoding="utf-8").split("\n"):
                        if not line.strip():
                            continue
                        urls.append(
                            json.loads(line)["url"]
                            if line.lstrip().startswith("{")
                            else line.strip()
                        )
                    output = search.import_urls(job, urls)
                    export.write_stage(job.state, job.root, "search", output)
                elif args.command == "metadata":
                    try:
                        metadata.run(
                            job, args.limit or topic.metadata.limit or 10**9, missing_only=True
                        )
                    except CollectionError:
                        print(
                            json.dumps(
                                jobs.read(job.root)["stages"]["metadata"], ensure_ascii=False
                            )
                        )
                        raise
                    output = jobs.read(job.root)["stages"]["metadata"]
                elif args.command == "filter":
                    output = filtering.run(job, args.limit, retry_failed=args.retry_failed)
                elif args.command == "export":
                    if args.output is None and args.format == "jsonl" and args.selection == "all":
                        output = export.write_stage(
                            job.state, job.root, "export", {"status": "complete"}
                        )
                    else:
                        destination = (
                            args.output or job.root / "exports" / f"{args.selection}.{args.format}"
                        )
                        output = export.selected(
                            job.state, job.root, destination, args.format, args.selection
                        )
                elif args.command == "download":
                    output = download.run(job, args.limit or topic.download.limit)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.command == "filter" and output.get("stopped"):
            return 2
        if args.command == "search":
            if isinstance(output, dict) and output.get("status") == "partial":
                return 2
            if isinstance(output, list) and output and all("error" in r for r in output):
                return 2
        return 0
    except CollectionError as exc:
        print(
            json.dumps({"stage": exc.stage, "error": exc.code}, ensure_ascii=False), file=sys.stderr
        )
        return 130 if exc.code == "interrupted" else 2
    except (OSError, ValueError, KeyError, ValidationError):
        print(json.dumps({"error": "invalid_config_or_input_file"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
