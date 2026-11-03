#!/usr/bin/env python3
"""Migrate v2 single-sheet screening to v3 pages without any API calls.

Preserve old judgments explicitly as legacy_whole_video evidence. The new config
is accepted for cache reuse, while original config hashes remain as provenance.
"""

import argparse
import copy
import json
import shutil
import sqlite3
from pathlib import Path

from video_collect import export, jobs, screening
from video_collect.config import load_topic
from video_collect.models import ScreenPage
from video_collect.state import State, dump


def convert(value, config_hash):
    value = copy.deepcopy(value)
    if "pages" in value:
        return value
    evidence = value.pop("evidence", None)
    result = value.get("result") or {}
    pages = []
    if evidence:
        evidence.update(
            page=1,
            start_seconds=0,
            end_seconds=evidence["duration_seconds"],
            sampling="legacy_whole_video",
        )
        page = ScreenPage(
            page=1,
            status="complete" if value["status"] == "complete" else "failed",
            error=value.get("error"),
            grade=result.get("grade"),
            reason=result.get("reason"),
            evidence_frames=result.get("evidence_frames") or [],
            evidence=evidence,
        )
        pages.append(page.model_dump(mode="json"))
    value["pages"] = pages
    value["result"] = screening.summarize_pages(pages)
    value["accepted_config_hash"] = config_hash
    value["requested_calls"] = value.get("requested_calls", int(value.get("status") == "complete"))
    return value


def migrate(root, topic_path, backup):
    root, backup = root.resolve(), backup.resolve()
    with jobs.locked(root):
        before = [json.loads(line) for line in (root / "videos.jsonl").open() if line.strip()]
        if all(r["schema"] == "video_collect.video.v3" for r in before):
            return {"status": "already_migrated"}
        if any(r["schema"] != "video_collect.video.v2" for r in before):
            raise ValueError("expected a v2 dataset")
        if not (root / ".state/state.sqlite").is_file():
            raise ValueError("migration requires the original SQLite state")
        state = State(root / ".state/state.sqlite")
        original_task = jobs.read(root)
        topic = load_topic(topic_path)
        if topic.id != original_task["topic"]["id"]:
            raise ValueError("topic mismatch")
        # Respect the model used by this task unless the environment explicitly overrides it.
        config = screening.screen_descriptor(topic)
        if (
            original_task.get("screen_config", {}).get("descriptor", {}).get("model")
            != config["descriptor"]["model"]
        ):
            raise ValueError("model differs from existing task; migrate with the same model")
        backup.mkdir(parents=True, exist_ok=False)
        for name in ("videos.jsonl", "task.json"):
            shutil.copy2(root / name, backup / name)
        with (
            sqlite3.connect(state.path) as source,
            sqlite3.connect(backup / "state.sqlite") as target,
        ):
            source.backup(target)
        values = state.screens()
        converted = {key: convert(value, config["hash"]) for key, value in values.items()}
        with state.connect() as db:
            for key, value in converted.items():
                db.execute(
                    "UPDATE screen_results SET data_json=? WHERE resource_key=?", (dump(value), key)
                )
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('screen_config', ?)", (dump(config),)
            )
        try:
            after, _ = export.snapshot(state, root)
            assert len(before) == len(after)
            original_by_id = {r["record_id"]: r for r in before}
            for record in after:
                old = original_by_id[record["record_id"]]
                assert {k: v for k, v in old.items() if k not in {"schema", "screening"}} == {
                    k: v for k, v in record.items() if k not in {"schema", "screening"}
                }
                assert old["screening"]["status"] == record["screening"]["status"]
                assert old["screening"]["error"] == record["screening"]["error"]
                assert (old["screening"].get("data") or {}).get("grade") == (
                    record["screening"].get("data") or {}
                ).get("grade")
                if (old["screening"].get("data") or {}).get("evidence"):
                    page = record["screening"]["data"]["pages"][0]
                    assert (
                        page["evidence"]["sha256"] == old["screening"]["data"]["evidence"]["sha256"]
                    )
                    assert page["reason"] == old["screening"]["data"].get("reason")
            summary = original_task["stages"].get("screen", {}).copy()
            summary["cumulative"] = screening.usage_totals(converted.values())
            summary["migration"] = "v3_pages_preserve_legacy"
            export.write_stage(state, root, "screen", summary)
        except BaseException:
            with state.connect() as db:
                for key, value in values.items():
                    db.execute(
                        "UPDATE screen_results SET data_json=? WHERE resource_key=?",
                        (dump(value), key),
                    )
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('screen_config', ?)",
                    (dump(original_task["screen_config"]),),
                )
            for name in ("videos.jsonl", "task.json"):
                shutil.copy2(backup / name, root / name)
            raise
        return {
            "status": "migrated",
            "records": len(after),
            "screen_records": len(converted),
            "pages": sum(len(v["pages"]) for v in converted.values()),
            "backup": str(backup),
            "unchanged_other_stages": True,
            "api_calls": 0,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--topic", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(migrate(args.input, args.topic, args.backup), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
