"""Portable video manifest, task checkpoints and explicit convenience exports."""

import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from .media import sha256
from .models import (
    CollectionError,
    MediaInfo,
    ResourceIdentity,
    VideoMetadata,
    VideoRecord,
)
from .state import State, compact_search, dump, now, stage_attempts


def _atomic_write(records: list[dict], path: Path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        file.flush()
        os.fsync(file.fileno())
    tmp.replace(path)


def validate_downloaded(records: list[dict], root: Path) -> int:
    """Verify archived originals without writing a second inventory."""
    root = root.resolve()
    exported = []
    for row in records:
        info = MediaInfo.model_validate_json(row["media_json"])
        if not info.path.resolve().is_relative_to(root / "raw"):
            continue
        if (
            not info.path.is_file()
            or info.path.stat().st_size != info.bytes
            or sha256(info.path) != info.sha256
        ):
            raise CollectionError("downloaded_artifact_inconsistent", "export")
        exported.append(row["key"])
    return len(exported)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        file.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def _paths(value, root: Path, *, portable: bool):
    """Translate only media/evidence/input paths, never arbitrary metadata strings."""

    def convert(text):
        path = Path(text)
        path = path.resolve() if path.is_absolute() else (root / path).resolve()
        if not path.is_relative_to(root):
            raise CollectionError("task_asset_outside_directory", "export")
        return str(path.relative_to(root)) if portable else str(path)

    if isinstance(value, list):
        return [_paths(v, root, portable=portable) for v in value]
    if not isinstance(value, dict):
        return value
    return {
        k: convert(v) if k == "path" and isinstance(v, str) else _paths(v, root, portable=portable)
        for k, v in value.items()
    }


def _status(value: str) -> str:
    if value in ("complete", "completed", "verified"):
        return "complete"
    if value in ("failed", "missing", "outcome_unknown"):
        return "failed"
    return "stale" if value == "stale" else "not_run"


def snapshot(state: State, root: Path, *, include_resume=True) -> tuple[list[dict], dict]:
    """One record per resource, carrying provenance and every completed stage."""
    with state.connect() as db:
        tables = {
            name: [dict(r) for r in db.execute(f"SELECT * FROM {name}")]
            if include_resume or name not in {"record_aliases", "searches", "filter_batches"}
            else []
            for name in (
                "resources",
                "metadata_shared",
                "aliases",
                "record_aliases",
                "discoveries",
                "artifacts",
                "searches",
                "text_filters",
                "screen_results",
                "filter_batches",
            )
        }
    origins, aliases, record_aliases = (defaultdict(list) for _ in range(3))
    for row in tables["discoveries"]:
        origins[row["resource_key"]].append(
            {**json.loads(row["candidate_json"]), "discovered_at": row["discovered_at"]}
        )
    for row in tables["aliases"]:
        aliases[row["resource_key"]].append(row["url"])
    for row in tables["record_aliases"]:
        record_aliases[row["resource_key"]].append(row["record_id"])
    from .filtering import current as current_filter
    from .filtering import error_summary

    filters = {r["resource_key"]: json.loads(r["data_json"]) for r in tables["text_filters"]}
    artifacts = {r["resource_key"]: r for r in tables["artifacts"]}
    screens = {r["resource_key"]: json.loads(r["data_json"]) for r in tables["screen_results"]}
    screen_config = json.loads(state.setting("screen_config") or "null")
    from .metadata import validate_parts

    shared = {r["reference"]: json.loads(r["data_json"]) for r in tables["metadata_shared"]}
    records, resume_records = [], {}
    for row in sorted(tables["resources"], key=lambda r: r["record_id"]):
        key = row["key"]
        candidate = json.loads(row["candidate_json"])
        details = json.loads(row["metadata_json"]) if row["metadata_json"] else None
        if details:
            validate_parts(VideoMetadata.model_validate(details), shared)
        artifact = artifacts.get(key, {})
        info = json.loads(artifact["media_json"]) if artifact else None
        errors = json.loads(row["stage_errors_json"])
        download_status = "not_run"
        if info:
            path = Path(info["path"])
            download_status = "complete" if path.is_relative_to(root / "raw") else "verified"
            if not path.is_file():
                download_status = "missing"
        if errors.get("download"):
            download_status = "failed"
        screen_status = "not_run"
        text_filter = current_filter(filters.get(key), row)
        filter_status = _status(text_filter["status"])
        filter_data = None
        if text_filter.get("decision"):
            filter_data = {
                "decision": text_filter["decision"],
                "reason": (text_filter.get("result") or {}).get("reason")
                if filter_status == "complete"
                else None,
            }
        screen_data = None
        from .screening import current_screen

        preview_screen = current_screen(screens.get(key), row, (screen_config or {}).get("hash"))
        screen_error = errors.get("screening")
        if key in screens:
            screen_status = preview_screen["status"]
            screen_error = preview_screen.get("error")
            screen_data = {
                "grade": None,
                **(preview_screen.get("result") or {}),
                "pages": _paths(preview_screen.get("pages") or [], root, portable=True),
            }
        record = VideoRecord.model_validate(
            {
                "record_id": row["record_id"],
                "identity": {**json.loads(row["identity_json"]), "aliases": sorted(aliases[key])},
                "search": {"status": "complete", "error": None, "data": origins[key]},
                "metadata": {
                    "status": "complete"
                    if details
                    else ("failed" if errors.get("metadata") else "not_run"),
                    "error": errors.get("metadata"),
                    "data": {k: v for k, v in details.items() if k != "identity"}
                    if details
                    else None,
                },
                "filter": {
                    "status": filter_status,
                    "error": text_filter.get("error"),
                    "data": filter_data,
                },
                "download": {
                    "status": _status(download_status),
                    "error": errors.get("download")
                    or ("media_file_missing" if download_status == "missing" else None),
                    "data": _paths(info, root, portable=True),
                },
                "screening": {
                    "status": _status(screen_status),
                    "error": screen_error,
                    "data": screen_data,
                },
            }
        ).model_dump(mode="json", by_alias=True)
        records.append(record)
        if not include_resume:
            continue
        # Execution details live in the task sidecar, keyed by stable record ID.
        resume_records[row["record_id"]] = {
            "candidate": candidate,
            "merged_record_ids": sorted(set(record_aliases[key]) - {row["record_id"]}),
            "processing": {
                "status": row["status"],
                "attempts": row["attempts"],
                "stage_attempts": stage_attempts(row),
                "error": row["error_code"],
                "updated_at": row["updated_at"],
            },
            "filter": text_filter,
            "preview_screen": _paths(screens.get(key), root, portable=True),
        }
    if not include_resume:
        return records, {}
    checkpoints = [
        {
            "request": json.loads(r["request_json"]),
            "result": compact_search(json.loads(r["result_json"])) if r["result_json"] else None,
            "error": r["error_code"],
            "created_at": r["created_at"],
        }
        for r in tables["searches"]
    ]
    return records, {
        "screen_config": screen_config,
        "metadata_shared": shared,
        "resume_records": resume_records,
        "checkpoints": checkpoints,
        "filter_batches": [json.loads(r["data_json"]) for r in tables["filter_batches"]],
        "filter_errors": error_summary(filters),
    }


def write_stage(state: State, root: Path, stage: str, summary: dict) -> dict:
    root = root.resolve()
    records, resume = snapshot(state, root)
    latest = {dump(c["request"]): c for c in resume["checkpoints"]}
    error_stage = "screening" if stage == "screen" else stage
    errors = [
        r[error_stage]["error"] for r in records if error_stage in r and r[error_stage].get("error")
    ]
    if stage == "search":
        errors.extend(c["error"] for c in latest.values() if c["error"])
    summary = {
        **summary,
        "stage": stage,
        "updated_at": now(),
        "candidate_records": len(records),
        "metadata_records": sum(r["metadata"]["data"] is not None for r in records),
        "states": state.counts(),
        "errors": dict(Counter(errors)),
        "historical_error_records": sum(c["error"] is not None for c in resume["checkpoints"]),
    }
    if stage == "metadata":
        pending = len(records) - summary["metadata_records"]
        summary.update(
            pending_records=pending,
            status="partial"
            if errors or summary.get("stopped")
            else ("pending" if pending else "complete"),
        )
    from .jobs import read

    task = read(root)
    task["schema"] = "video_collect.task.v2"
    task["search"]["checkpoints"] = resume.pop("checkpoints")
    task.update(resume)
    if task.get("screen_config"):
        refs = task["topic"]["screen"].get("references", [])
        task["topic"]["screen"] = {**task["screen_config"]["settings"], "references": refs}
        task["topic"]["download"]["grades"] = task["screen_config"]["download_grades"]
    filter_progress = state.setting("filter_progress")
    if filter_progress:
        task["stages"]["filter"] = json.loads(filter_progress)
    task["stages"][stage] = summary
    task["updated_at"] = now()
    # Publish records before cursors: a crash can repeat a page, never skip unsaved videos.
    _atomic_write(records, root / "videos.jsonl")
    task["videos_sha256"] = sha256(root / "videos.jsonl")
    write_json(root / "task.json", task)
    return summary


def restore(state: State, root: Path):
    """Rebuild disposable execution state from the two handoff files under the run lock."""
    root = root.resolve()
    previous_root = state.setting("root")
    if previous_root == str(root):
        return
    if previous_root is not None:
        # SQLite may contain confirmed results newer than the last exported pair.
        # Moving a task must rebase asset paths, never replace that state with a snapshot.
        old_root = Path(previous_root)
        with state.connect() as db:
            for table, column in (("artifacts", "media_json"), ("screen_results", "data_json")):
                for row in db.execute(f"SELECT resource_key,{column} FROM {table}").fetchall():
                    value = _paths(json.loads(row[column]), old_root, portable=True)
                    value = _paths(value, root, portable=False)
                    db.execute(
                        f"UPDATE {table} SET {column}=? WHERE resource_key=?",
                        (dump(value), row["resource_key"]),
                    )
                    if table == "artifacts" and not Path(value["path"]).is_file():
                        db.execute(
                            "UPDATE resources SET status='metadata_ready' WHERE key=? "
                            "AND status IN ('downloaded','processing','interrupted')",
                            (row["resource_key"],),
                        )
            db.execute("UPDATE settings SET value=? WHERE key='root'", (str(root),))
        return
    # Direct library users can add candidates before entering the first run lock.
    if previous_root is None and state.rows():
        state.setting("root", str(root))
        return
    from .jobs import read

    task = read(root)
    if task.get("schema") != "video_collect.task.v2":
        raise CollectionError("invalid_task_directory", "job")
    actual_hash = sha256(root / "videos.jsonl")
    if task.get("videos_sha256") != actual_hash and (root / "videos.jsonl").stat().st_size:
        # The sidecar now carries per-record execution state: never silently
        # combine two generations and reissue a possibly paid model request.
        raise CollectionError("task_snapshot_mismatch_export_from_original_state", "job")
    # JSON strings may contain Unicode line separators (e.g. U+2028).
    # str.splitlines() treats those as record boundaries; JSONL uses LF only.
    with (root / "videos.jsonl").open(encoding="utf-8") as file:
        records = [json.loads(line) for line in file if line.strip()]
    from .metadata import parts_reference, validate_parts
    from .models import Candidate, ResourceIdentity

    shared = task.get("metadata_shared", {})
    for reference, value in shared.items():
        if not isinstance(value, dict) or parts_reference(value) != reference:
            raise CollectionError("shared_parts_missing_or_invalid", "job")

    with state.connect() as db:
        for table in (
            "aliases",
            "record_aliases",
            "discoveries",
            "artifacts",
            "text_filters",
            "screen_results",
            "filter_batches",
            "resources",
            "metadata_shared",
            "searches",
            "settings",
        ):
            db.execute(f"DELETE FROM {table}")
        for reference, value in shared.items():
            db.execute("INSERT INTO metadata_shared VALUES (?,?)", (reference, dump(value)))
        for record in records:
            try:
                record = VideoRecord.model_validate(record).model_dump(mode="json", by_alias=True)
                saved = task["resume_records"][record["record_id"]]
            except (ValueError, KeyError):
                raise CollectionError("invalid_video_record", "job") from None
            identity = ResourceIdentity.model_validate(
                {k: v for k, v in record["identity"].items() if k != "aliases"}
            )
            candidate = Candidate.model_validate(saved["candidate"])
            key = identity.key
            if candidate.platform != identity.platform:
                raise CollectionError("record_identity_mismatch", "job")
            details = record["metadata"]["data"]
            if details:
                details = VideoMetadata.model_validate(
                    {**details, "identity": identity.model_dump(mode="json")}
                ).model_dump(mode="json")
            if details:
                validate_parts(VideoMetadata.model_validate(details), shared)
            progress = saved["processing"]
            status = progress["status"]
            info = _paths(record["download"]["data"], root, portable=False)
            if info:
                MediaInfo.model_validate(info)
                if not Path(info["path"]).is_file() and status in (
                    "downloaded",
                    "processing",
                    "interrupted",
                ):
                    status = "metadata_ready"  # A manifest cannot substitute for media files.
            stage_errors = {
                name: record[name]["error"]
                for name in ("metadata", "download", "screening")
                if record[name]["error"]
            }
            db.execute(
                "INSERT INTO resources (key,record_id,platform,identity_json,candidate_json,metadata_json,status,attempts,error_code,updated_at,stage_errors_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    record["record_id"],
                    identity.platform,
                    identity.model_dump_json(),
                    candidate.model_dump_json(),
                    dump(details) if details else None,
                    status,
                    progress["attempts"],
                    progress["error"],
                    progress["updated_at"],
                    dump(stage_errors),
                ),
            )
            retries = progress.get("stage_attempts")
            if retries is None:
                retries = stage_attempts(
                    {
                        "attempts": progress["attempts"],
                        "metadata_json": details,
                        "stage_errors_json": dump(stage_errors),
                    }
                )
            db.execute(
                "UPDATE resources SET stage_attempts_json=? WHERE key=?",
                (dump(retries), key),
            )
            if saved.get("preview_screen"):
                db.execute(
                    "INSERT INTO screen_results VALUES (?,?)",
                    (key, dump(_paths(saved["preview_screen"], root, portable=False))),
                )
            text_filter = saved["filter"]
            if text_filter.get("input_hash"):
                db.execute("INSERT INTO text_filters VALUES (?,?)", (key, dump(text_filter)))
            for rid in [record["record_id"], *saved["merged_record_ids"]]:
                db.execute("INSERT INTO record_aliases VALUES (?,?)", (rid, key))
            for url in record["identity"]["aliases"]:
                db.execute("INSERT INTO aliases VALUES (?,?)", (url, key))
            for match in record["search"]["data"] or []:
                match = dict(match)
                when = match.pop("discovered_at")
                value = Candidate.model_validate(match).model_dump_json()
                db.execute(
                    "INSERT OR IGNORE INTO discoveries(resource_key,candidate_json,discovered_at) VALUES (?,?,?)",
                    (key, value, when),
                )
            if info:
                db.execute(
                    "INSERT INTO artifacts VALUES (?,?)",
                    (key, dump(info)),
                )
        # On a crash between the two atomic file replacements, replay search safely.
        if task.get("videos_sha256") == sha256(root / "videos.jsonl"):
            for checkpoint in task["search"]["checkpoints"]:
                db.execute(
                    "INSERT INTO searches(request_json,result_json,error_code,created_at) VALUES (?,?,?,?)",
                    (
                        dump(checkpoint["request"]),
                        dump(checkpoint["result"]) if checkpoint["result"] else None,
                        checkpoint["error"],
                        checkpoint["created_at"],
                    ),
                )
        for batch in task.get("filter_batches", []):
            db.execute("INSERT INTO filter_batches VALUES (?,?)", (batch["id"], dump(batch)))
        if task.get("screen_config"):
            db.execute(
                "INSERT INTO settings VALUES ('screen_config',?)", (dump(task["screen_config"]),)
            )
        if task.get("stages", {}).get("filter"):
            db.execute(
                "INSERT INTO settings VALUES ('filter_progress',?)",
                (dump(task["stages"]["filter"]),),
            )
        db.execute("INSERT INTO settings VALUES ('root',?)", (str(root),))


def selected(state: State, root: Path, destination: Path, format: str, selection: str) -> dict:
    """Optional convenience export; never overwrite the authoritative task files."""
    destination = destination.resolve()
    if destination in (
        (root / "videos.jsonl").resolve(),
        (root / "task.json").resolve(),
    ) or any(
        destination.is_relative_to(root / directory)
        for directory in (".state", "raw", "evidence", "references")
    ):
        raise CollectionError("export_would_overwrite_task", "export")
    records, resume = snapshot(state, root)
    if selection == "accepted":
        validate_downloaded(state.downloaded_records(), root)
        allowed = (resume.get("screen_config") or {}).get("download_grades", ["priority"])
        records = [
            r
            for r in records
            if (
                r["download"]["status"] == "complete"
                and r["screening"]["status"] == "complete"
                and (r["screening"]["data"] or {}).get("grade") in allowed
                and r["filter"]["status"] == "complete"
                and (r["filter"]["data"] or {}).get("decision") == "keep"
            )
        ]
    elif selection == "downloaded":
        validate_downloaded(state.downloaded_records(), root)
        records = [
            r
            for r in records
            if r["download"]["status"] == "complete"
            and Path(r["download"]["data"]["path"]).is_relative_to("raw")
        ]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if format == "jsonl":
        _atomic_write(records, destination)
    else:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
                    "record_id",
                    "resource_key",
                    "platform",
                    "url",
                    "title",
                    "metadata_status",
                    "download_status",
                    "filter_decision",
                    "screen_grade",
                    "path",
                ],
            )
            writer.writeheader()
            for record in records:
                details = record["metadata"]["data"] or {}
                writer.writerow(
                    {
                        "record_id": record["record_id"],
                        "resource_key": ResourceIdentity.model_validate(
                            {k: v for k, v in record["identity"].items() if k != "aliases"}
                        ).key,
                        "platform": record["identity"]["platform"],
                        "url": details.get("access_url") or record["identity"]["canonical_url"],
                    }
                    | {
                        "title": details.get("title")
                        or resume["resume_records"][record["record_id"]]["candidate"].get("title"),
                        "metadata_status": record["metadata"]["status"],
                        "download_status": record["download"]["status"],
                        "filter_decision": (record["filter"]["data"] or {}).get("decision"),
                        "screen_grade": (record["screening"]["data"] or {}).get("grade"),
                        "path": (record["download"]["data"] or {}).get("path"),
                    }
                )
        temporary.replace(destination)
    return {"exported_records": len(records), "output": str(destination)}
