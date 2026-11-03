#!/usr/bin/env python3
"""Build a read-only, offline HTML view of a video_collect v3 dataset.

No API requests, database writes, or media downloads. Existing local evidence is
linked relative to the HTML; keep the report and dataset in their current paths.
"""

import argparse
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse


def web_url(value):
    return value if isinstance(value, str) and urlparse(value).scheme in {"http", "https"} else None


def local_url(value, root, output):
    if not value:
        return None
    path = (root / value).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return quote(os.path.relpath(path, output.parent), safe="/")


def project(record, root, output):
    identity = record["identity"]
    metadata = record["metadata"].get("data") or {}
    hits = record["search"].get("data") or []
    filtering = record["filter"]
    screening = record["screening"]
    visual = screening.get("data") or {}
    pages = []
    for page in visual.get("pages") or []:
        evidence = page["evidence"]
        pages.append(
            {k: v for k, v in page.items() if k != "evidence"}
            | {
                "preview": local_url(evidence.get("path"), root, output),
                "frames": evidence.get("frames") or [],
                "cover_present": evidence.get("cover_present"),
                "start_seconds": evidence["start_seconds"],
                "end_seconds": evidence["end_seconds"],
                "sampling": evidence["sampling"],
            }
        )
    first = pages[0] if pages else {}
    media = record["download"].get("data") or {}
    return {
        "id": record["record_id"],
        "platform": identity["platform"],
        "part": identity.get("part_id"),
        "title": metadata.get("title")
        or next((h.get("title") for h in hits if h.get("title")), "未获取标题"),
        "author": metadata.get("author"),
        "duration": metadata.get("duration_seconds"),
        "description": metadata.get("description"),
        "tags": metadata.get("tags") or [],
        "queries": list(dict.fromkeys(h["query"] for h in hits if h.get("query"))),
        "url": web_url(metadata.get("access_url") or identity.get("canonical_url")),
        "cover": next((u for u in metadata.get("thumbnails", []) if web_url(u)), None),
        "filter": filtering,
        "screen": {k: v for k, v in screening.items() if k != "data"}
        | {"data": {k: v for k, v in visual.items() if k != "pages"}},
        "pages": pages,
        "preview": first.get("preview"),
        "frames": first.get("frames", []),
        "cover_present": first.get("cover_present"),
        "metadata_status": record["metadata"]["status"],
        "download_status": record["download"]["status"],
        "errors": {
            s: record[s]["error"]
            for s in ("search", "metadata", "filter", "screening", "download")
            if record[s].get("error")
        },
        "media": local_url(media.get("path"), root, output),
    }


def build(source: Path, output: Path):
    source = source.resolve()
    source = source / "videos.jsonl" if source.is_dir() else source
    root = source.parent
    output = output.resolve()
    if output.suffix.lower() != ".html":
        raise ValueError("output must end with .html")
    records = []
    with source.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("schema") != "video_collect.video.v3":
                raise ValueError(f"line {number}: expected video_collect.video.v3")
            records.append(project(record, root, output))
    records.sort(key=lambda r: r["id"])
    task_path = root / "task.json"
    task = json.loads(task_path.read_text(encoding="utf-8")) if task_path.exists() else {}
    stages = task.get("stages") or {}
    payload = {
        "name": root.name,
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(source),
        "usage": {
            s: {
                k: v
                for k, v in (stages.get(s) or {}).items()
                if k
                in {
                    "usage",
                    "cumulative",
                    "usage_reported_calls",
                    "usage_missing_calls",
                    "calls",
                    "updated_at",
                }
            }
            for s in ("filter", "screen")
        },
        "records": records,
    }
    # Prevent titles/descriptions containing HTML from terminating the JSON script.
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = Path(__file__).with_name("dataset_viewer.html").read_text(encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.replace("__DATASET_JSON__", data), encoding="utf-8")
    return {
        "output": str(output),
        "records": len(records),
        "filter": dict(
            Counter(
                (r["filter"].get("data") or {}).get("decision") or r["filter"]["status"]
                for r in records
            )
        ),
        "screen": dict(
            Counter(
                ((r["screen"].get("data") or {}).get("grade") or "complete")
                if r["screen"]["status"] == "complete"
                else r["screen"]["status"]
                for r in records
            )
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", required=True, type=Path, help="Dataset directory or videos.jsonl"
    )
    parser.add_argument("--output", type=Path, default=Path("tmp/dataset-viewer/index.html"))
    args = parser.parse_args()
    print(json.dumps(build(args.input, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
