"""SQLite persistence and atomic resource merges. No stage scheduling."""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import Candidate, MediaInfo, ResourceIdentity, VideoMetadata

TERMINAL = {"rejected"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stage_attempts(row: dict) -> dict:
    """Old totals cannot be split exactly; attribute them only to the known stage."""
    if row.get("stage_attempts_json") is not None:
        return json.loads(row["stage_attempts_json"])
    errors = json.loads(row.get("stage_errors_json") or "{}")
    stage = (
        "metadata"
        if not row.get("metadata_json")
        else ("download" if errors.get("download") else None)
    )
    return {stage: row["attempts"]} if stage else {}


def compact_search(result: dict) -> dict:
    if "candidate_count" in result:
        return result
    from .identity import identify

    keys = sorted(identify(c["url"]).key for c in result["candidates"])
    return {
        "candidate_count": len(keys),
        "signature": hashlib.sha256(dump(keys).encode()).hexdigest(),
        "pagination": result["pagination"],
        "next_page": result.get("next_page"),
        "stop_reason": result["stop_reason"],
    }


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata_shared (
                    reference TEXT PRIMARY KEY, data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    key TEXT PRIMARY KEY, record_id TEXT NOT NULL, platform TEXT NOT NULL, identity_json TEXT NOT NULL,
                    candidate_json TEXT NOT NULL, metadata_json TEXT,
                    status TEXT NOT NULL DEFAULT 'discovered', attempts INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, updated_at TEXT NOT NULL,
                    stage_errors_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS record_aliases (
                    record_id TEXT PRIMARY KEY, resource_key TEXT NOT NULL REFERENCES resources(key)
                );
                CREATE TABLE IF NOT EXISTS aliases (
                    url TEXT PRIMARY KEY, resource_key TEXT NOT NULL REFERENCES resources(key)
                );
                CREATE TABLE IF NOT EXISTS discoveries (
                    id INTEGER PRIMARY KEY, resource_key TEXT NOT NULL REFERENCES resources(key),
                    candidate_json TEXT NOT NULL, discovered_at TEXT NOT NULL,
                    UNIQUE(resource_key, candidate_json)
                );
                CREATE TABLE IF NOT EXISTS searches (
                    id INTEGER PRIMARY KEY, request_json TEXT NOT NULL, result_json TEXT,
                    error_code TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    resource_key TEXT PRIMARY KEY REFERENCES resources(key),
                    media_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS text_filters (
                    resource_key TEXT PRIMARY KEY REFERENCES resources(key), data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS screen_results (
                    resource_key TEXT PRIMARY KEY REFERENCES resources(key), data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS filter_batches (id TEXT PRIMARY KEY, data_json TEXT NOT NULL);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(resources)")}
            if "stage_attempts_json" not in columns:
                db.execute("ALTER TABLE resources ADD COLUMN stage_attempts_json TEXT")
            # Materialize legacy counters once instead of guessing on every stage read.
            for row in db.execute(
                "SELECT * FROM resources WHERE stage_attempts_json IS NULL"
            ).fetchall():
                db.execute(
                    "UPDATE resources SET stage_attempts_json=? WHERE key=?",
                    (dump(stage_attempts(dict(row))), row["key"]),
                )

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    def add(self, candidate: Candidate, identity: ResourceIdentity) -> tuple[str, bool]:
        data = candidate.model_dump_json()
        with self.connect() as db:
            alias = db.execute(
                "SELECT resource_key FROM aliases WHERE url=?", (candidate.url,)
            ).fetchone()
            key = alias[0] if alias else identity.key
            inserted = db.execute(
                "INSERT OR IGNORE INTO resources "
                "(key,record_id,platform,identity_json,candidate_json,updated_at,stage_attempts_json) VALUES (?,?,?,?,?,?,'{}')",
                (key, uuid4().hex, candidate.platform, identity.model_dump_json(), data, now()),
            ).rowcount
            db.execute(
                "INSERT OR IGNORE INTO record_aliases SELECT record_id,key FROM resources WHERE key=?",
                (key,),
            )
            db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (candidate.url, key))
            db.execute(
                "INSERT OR IGNORE INTO discoveries(resource_key,candidate_json,discovered_at) VALUES (?,?,?)",
                (key, data, now()),
            )
            return key, bool(inserted)

    def get(self, key: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM resources WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            return dict(row)

    def rows(
        self,
        *,
        max_attempts: int | None = None,
        attempt_stage: str | None = None,
        limit: int | None = None,
        exclude_statuses: tuple[str, ...] | None = None,
        missing_metadata: bool = False,
        require_metadata: bool = False,
        exclude_filtered: bool = False,
        keep_only: bool = False,
        screened_grades: list[str] | None = None,
        max_duration_seconds: float | None = None,
    ) -> list[dict]:
        with self.connect() as db:
            if max_attempts is None:
                query, params = "SELECT * FROM resources ORDER BY record_id", ()
            else:
                excluded = exclude_statuses if exclude_statuses is not None else tuple(TERMINAL)
                placeholders = ",".join("?" for _ in excluded)
                query = f"SELECT * FROM resources WHERE status NOT IN ({placeholders}) "
                if attempt_stage is None:
                    query += "AND attempts < ? ORDER BY record_id"
                    params = (*excluded, max_attempts)
                else:
                    query += "ORDER BY record_id"
                    params = tuple(excluded)
            if missing_metadata:
                # Insert the predicate before the existing ordering and LIMIT.
                before, ordering = query.split(" ORDER BY ", 1)
                query = before + (" AND " if " WHERE " in before else " WHERE ")
                query += "metadata_json IS NULL ORDER BY " + ordering
            if require_metadata:
                before, ordering = query.split(" ORDER BY ", 1)
                query = before + (" AND " if " WHERE " in before else " WHERE ")
                query += "metadata_json IS NOT NULL ORDER BY " + ordering
            if max_duration_seconds is not None:
                before, ordering = query.split(" ORDER BY ", 1)
                query = before + (" AND " if " WHERE " in before else " WHERE ")
                query += "json_extract(metadata_json, '$.duration_seconds') IS NOT NULL "
                query += (
                    "AND CAST(json_extract(metadata_json, '$.duration_seconds') AS REAL) <= ? ORDER BY "
                    + ordering
                )
                params = (*params, max_duration_seconds)
            if (
                limit is not None
                and not exclude_filtered
                and not keep_only
                and screened_grades is None
                and attempt_stage is None
            ):
                query += " LIMIT ?"
                params = (*params, limit)
            rows = [dict(row) for row in db.execute(query, params)]
            if attempt_stage is not None and max_attempts is not None:
                rows = [r for r in rows if stage_attempts(r).get(attempt_stage, 0) < max_attempts]
            if exclude_filtered or keep_only or screened_grades is not None:
                from .filtering import current

                filters = self.text_filters()
                from .screening import current_screen

                screens = self.screens() if screened_grades is not None else {}
                config_hash = json.loads(self.setting("screen_config") or "{}").get("hash")
                selected = []
                for row in rows:
                    result = current(filters.get(row["key"]), row)
                    if keep_only:
                        eligible = (
                            result.get("status") == "completed" and result.get("decision") == "keep"
                        )
                    else:
                        eligible = result.get("decision") != "reject"
                    if eligible and screened_grades is not None:
                        visual = current_screen(screens.get(row["key"]), row, config_hash)
                        eligible = (
                            visual["status"] == "complete"
                            and (visual.get("result") or {}).get("grade") in screened_grades
                        )
                    if eligible:
                        selected.append(row)
                rows = selected
                rows = rows[:limit] if limit is not None else rows
            return rows[:limit] if limit is not None else rows

    def completed_search(self, request: dict) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT result_json FROM searches WHERE request_json=? AND error_code IS NULL "
                "AND result_json IS NOT NULL ORDER BY id DESC LIMIT 1",
                (dump(request),),
            ).fetchone()
        return compact_search(json.loads(row[0])) if row else None

    def merge_metadata(self, old_key: str, metadata: VideoMetadata) -> str:
        from .metadata import compact_storage, validate_parts

        metadata, shared = compact_storage(metadata)
        key = metadata.identity.key
        with self.connect() as db:
            old = db.execute("SELECT * FROM resources WHERE key=?", (old_key,)).fetchone()
            if old is None:
                return key
            if old["stage_attempts_json"] is None:
                db.execute(
                    "UPDATE resources SET stage_attempts_json=? WHERE key=?",
                    (dump(stage_attempts(dict(old))), old_key),
                )
                old = db.execute("SELECT * FROM resources WHERE key=?", (old_key,)).fetchone()
            for reference, value in shared.items():
                db.execute(
                    "INSERT OR IGNORE INTO metadata_shared VALUES (?,?)", (reference, dump(value))
                )
            reference = metadata.raw.get("parts_ref")
            if reference:
                stored = db.execute(
                    "SELECT data_json FROM metadata_shared WHERE reference=?", (reference,)
                ).fetchone()
                validate_parts(metadata, {reference: json.loads(stored[0])} if stored else {})
            if key != old_key:
                existing = db.execute("SELECT key FROM resources WHERE key=?", (key,)).fetchone()
                if not existing:
                    db.execute(
                        "INSERT INTO resources "
                        "(key,record_id,platform,identity_json,candidate_json,metadata_json,status,attempts,error_code,updated_at,stage_errors_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            old["record_id"],
                            metadata.identity.platform,
                            metadata.identity.model_dump_json(),
                            old["candidate_json"],
                            metadata.model_dump_json(),
                            old["status"],
                            old["attempts"],
                            old["error_code"],
                            now(),
                            old["stage_errors_json"],
                        ),
                    )
                    db.execute(
                        "UPDATE resources SET stage_attempts_json=? WHERE key=?",
                        (old["stage_attempts_json"], key),
                    )
                db.execute(
                    "UPDATE record_aliases SET resource_key=? WHERE resource_key=?", (key, old_key)
                )
                db.execute("UPDATE aliases SET resource_key=? WHERE resource_key=?", (key, old_key))
                db.execute(
                    "UPDATE OR IGNORE discoveries SET resource_key=? WHERE resource_key=?",
                    (key, old_key),
                )
                db.execute("DELETE FROM discoveries WHERE resource_key=?", (old_key,))
                # Identity canonicalization occurs before any media or semantic work.
                for table in ("artifacts", "text_filters", "screen_results"):
                    db.execute(
                        f"UPDATE OR IGNORE {table} SET resource_key=? WHERE resource_key=?",
                        (key, old_key),
                    )
                    db.execute(f"DELETE FROM {table} WHERE resource_key=?", (old_key,))
                db.execute("DELETE FROM resources WHERE key=?", (old_key,))
            # A rediscovery must not silently rewrite the source used for an old judgment.
            db.execute(
                "UPDATE resources SET identity_json=?,metadata_json=COALESCE(metadata_json,?),updated_at=? WHERE key=?",
                (metadata.identity.model_dump_json(), metadata.model_dump_json(), now(), key),
            )
            db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (metadata.access_url, key))
            db.execute(
                "INSERT OR IGNORE INTO aliases VALUES (?,?)", (metadata.identity.canonical_url, key)
            )
        return key

    def update(
        self, key: str, status: str, error_code: str | None = None, *, attempt=False, stage=None
    ):
        with self.connect() as db:
            row = db.execute("SELECT * FROM resources WHERE key=?", (key,)).fetchone()
            errors = json.loads(row["stage_errors_json"]) if row else {}
            attempts = stage_attempts(dict(row)) if row else {}
            if attempt and stage:
                attempts[stage] = attempts.get(stage, 0) + 1
            if stage:
                if error_code:
                    errors[stage] = error_code
                else:
                    errors.pop(stage, None)
            db.execute(
                "UPDATE resources SET status=?,error_code=?,attempts=attempts+?,updated_at=?,stage_errors_json=?,stage_attempts_json=? WHERE key=?",
                (status, error_code, int(attempt), now(), dump(errors), dump(attempts), key),
            )

    def setting(self, key: str, value: str | None = None):
        with self.connect() as db:
            if value is not None:
                db.execute(
                    "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def search_result(self, request: dict, result: dict | None = None, error: str | None = None):
        with self.connect() as db:
            db.execute(
                "INSERT INTO searches(request_json,result_json,error_code,created_at) VALUES (?,?,?,?)",
                (dump(request), dump(result) if result else None, error, now()),
            )

    def artifact(self, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE resource_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def save_artifact(self, key: str, media: MediaInfo):
        with self.connect() as db:
            db.execute(
                "INSERT INTO artifacts(resource_key,media_json) VALUES (?,?) "
                "ON CONFLICT(resource_key) DO UPDATE SET media_json=excluded.media_json",
                (key, media.model_dump_json()),
            )

    def screens(self) -> dict:
        with self.connect() as db:
            return {
                r["resource_key"]: json.loads(r["data_json"])
                for r in db.execute("SELECT * FROM screen_results")
            }

    def save_screen(self, key: str, value: dict):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO screen_results VALUES (?,?)", (key, dump(value)))

    def text_filters(self) -> dict:
        with self.connect() as db:
            return {
                r["resource_key"]: json.loads(r["data_json"])
                for r in db.execute("SELECT * FROM text_filters")
            }

    def filter_descriptors(self) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT DISTINCT json_extract(data_json,'$.descriptor') FROM filter_batches"
                )
                if r[0]
            ]

    def save_text_filters(self, values: dict, batch: dict | None = None, *, progress=None):
        with self.connect() as db:
            for key, value in values.items():
                db.execute("INSERT OR REPLACE INTO text_filters VALUES (?,?)", (key, dump(value)))
            if batch:
                db.execute(
                    "INSERT OR REPLACE INTO filter_batches VALUES (?,?)", (batch["id"], dump(batch))
                )
            if progress is not None:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('filter_progress',?)",
                    (dump(progress),),
                )

    def recover(self):
        """Call only while holding the topic's exclusive run lock."""
        from .filtering import normalize_progress, normalize_result

        with self.connect() as db:
            progress = db.execute(
                "SELECT value FROM settings WHERE key='filter_progress'"
            ).fetchone()
            if progress:
                value = normalize_progress(json.loads(progress[0]))
                if value.get("status") == "running":
                    value.update(status="interrupted", stopped="interrupted", updated_at=now())
                if dump(value) != progress[0]:
                    db.execute(
                        "UPDATE settings SET value=? WHERE key='filter_progress'", (dump(value),)
                    )
            for row in db.execute(
                "SELECT text_filters.*,resources.record_id FROM text_filters "
                "JOIN resources ON resources.key=text_filters.resource_key"
            ).fetchall():
                value = json.loads(row["data_json"])
                if value["status"] == "in_flight":
                    value.update(
                        status="outcome_unknown",
                        decision="unclassified",
                        error="outcome_unknown",
                        updated_at=now(),
                    )
                value = normalize_result(value, row["record_id"])
                if dump(value) != row["data_json"]:
                    db.execute(
                        "UPDATE text_filters SET data_json=? WHERE resource_key=?",
                        (dump(value), row["resource_key"]),
                    )
            for row in db.execute("SELECT * FROM filter_batches").fetchall():
                value = json.loads(row["data_json"])
                if value["status"] == "in_flight":
                    value.update(
                        status="outcome_unknown", error="outcome_unknown", updated_at=now()
                    )
                    db.execute(
                        "UPDATE filter_batches SET data_json=? WHERE id=?", (dump(value), row["id"])
                    )
            for row in db.execute("SELECT * FROM screen_results").fetchall():
                value = json.loads(row["data_json"])
                if value["status"] == "in_flight":
                    value.update(status="failed", error="outcome_unknown", updated_at=now())
                    db.execute(
                        "UPDATE screen_results SET data_json=? WHERE resource_key=?",
                        (dump(value), row["resource_key"]),
                    )
            db.execute(
                "UPDATE resources SET status='interrupted',updated_at=? WHERE status='processing'",
                (now(),),
            )

    def counts(self) -> dict:
        with self.connect() as db:
            return {
                row[0]: row[1]
                for row in db.execute("SELECT status,count(*) FROM resources GROUP BY status")
            }

    def downloaded_records(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("""
                SELECT r.key,r.status,r.metadata_json,a.media_json
                FROM resources r JOIN artifacts a ON a.resource_key=r.key
                ORDER BY r.key
            """)
            ]
