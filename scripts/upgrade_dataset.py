#!/usr/bin/env python3
"""Upgrade current v3 datasets offline, preserving judgments and original audit records."""

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from video_collect import export, filtering, jobs
from video_collect.state import State

VERSION = "stage-retries-and-filter-cache-v1"


def upgrade(root: Path, backup: Path) -> dict:
    root, backup = root.resolve(), backup.resolve()
    if backup == root or backup.is_relative_to(root):
        raise ValueError("backup must be outside the dataset")
    database = root / ".state/state.sqlite"
    if not database.is_file():
        raise ValueError("upgrade requires the original SQLite database")
    with jobs.locked(root):
        task = jobs.read(root)
        if task.get("stages", {}).get("upgrade", {}).get("version") == VERSION:
            return {"status": "already_upgraded", "version": VERSION}
        # Back up before constructing State, whose initializer upgrades the SQL schema.
        backup.mkdir(parents=True, exist_ok=False)
        for name in ("videos.jsonl", "task.json"):
            shutil.copy2(root / name, backup / name)
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup / "state.sqlite") as target:
                source.backup(target)
        try:
            state = State(database)
            export.restore(state, root)
            if any(r["status"] == "processing" for r in state.rows()) or any(
                v["status"] == "in_flight"
                for v in [*state.text_filters().values(), *state.screens().values()]
            ):
                raise ValueError("recover interrupted work before upgrading")
            before, _ = export.snapshot(state, root)
            screens = state.screens()
            topic, _ = jobs.load(root, task=task)
            # Use the task's saved model, never a machine's model override.
            values = state.text_filters()
            new_hash, compatible = None, set()
            if values:
                if not topic.filter.prompt:
                    raise ValueError("existing filters require their saved task prompt")
                policy = filtering.descriptor(topic.filter.prompt, topic.filter, topic.filter.model)
                new_hash = filtering.policy_hash(policy)
                compatible = filtering.compatible_hashes(policy, state.filter_descriptors())
            changed = {}
            skipped = 0
            for row in state.rows():
                value = values.get(row["key"])
                if not value or value["config_hash"] == new_hash:
                    continue
                if not row["metadata_json"]:
                    skipped += 1
                    continue
                source = filtering.source(row)
                if (
                    value["config_hash"] not in compatible
                    or value["input_hash"] != filtering.input_hash(source, value["config_hash"])
                    or value.get("source_hash") != filtering.digest(source)
                ):
                    skipped += 1
                    continue
                changed[row["key"]] = {
                    **filtering.normalize_result(value, row["record_id"]),
                    "original_config_hash": value.get("original_config_hash", value["config_hash"]),
                    "original_input_hash": value.get("original_input_hash", value["input_hash"]),
                    "config_hash": new_hash,
                    "input_hash": filtering.input_hash(source, new_hash),
                }
            state.save_text_filters(changed)
            after, _ = export.snapshot(state, root)
            if before != after or screens != state.screens():
                raise ValueError("upgrade changed public records or visual evidence")
            summary = {
                "status": "complete",
                "version": VERSION,
                "records": len(after),
                "retry_counters_materialized": len(state.rows()),
                "filter_fingerprints_upgraded": len(changed),
                "incompatible_filters_untouched": skipped,
                "api_calls": 0,
                "backup": str(backup),
            }
            export.write_stage(state, root, "upgrade", summary)
            with state.connect() as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("database integrity check failed")
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError("database foreign key check failed")
            return summary
        except BaseException:
            # Every connection above is short-lived; restore the original database/schema.
            with sqlite3.connect(backup / "state.sqlite") as source:
                with sqlite3.connect(database) as target:
                    source.backup(target)
            for name in ("videos.jsonl", "task.json"):
                shutil.copy2(backup / name, root / name)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(upgrade(args.input, args.backup), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
