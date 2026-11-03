import json
import shutil
import sqlite3

import pytest

from video_curator import export, filtering, jobs, metadata, search


@pytest.fixture
def upgrade_module(load_script):
    return load_script("upgrade_dataset")


@pytest.fixture
def legacy_task(collection):
    job, details, _ = collection
    task = jobs.read(job.root)
    task["topic"]["filter"]["prompt"] = "Keep relevant videos."
    export.write_json(job.root / "task.json", task)
    job.topic.filter.prompt = task["topic"]["filter"]["prompt"]
    with job.exclusive():
        search.import_urls(job, [details.access_url])
        metadata.run(job, 1, missing_only=True)
        row = job.state.get(details.identity.key)
        source = filtering.source(row)
        descriptor = filtering.descriptor(
            job.topic.filter.prompt, job.topic.filter, job.topic.filter.model
        )
        descriptor.pop("prompt")
        descriptor.update(
            harness_version=1,
            system=job.topic.filter.prompt + "\n\n以下是批量文本审核协议。old protocol",
        )
        old_hash = filtering.digest(descriptor)
        value = {
            "status": "completed",
            "decision": "keep",
            "error": None,
            "result": {"record_id": row["record_id"], "decision": "keep", "reason": "relevant"},
            "config_hash": old_hash,
            "input_hash": filtering.input_hash(source, old_hash),
            "source_hash": filtering.digest(source),
            "batch_id": "old-batch",
        }
        job.state.save_text_filters(
            {row["key"]: value},
            {"id": "old-batch", "status": "completed", "descriptor": descriptor},
        )
        export.write_stage(job.state, job.root, "filter", {"calls": 1})
    # Simulate the pre-upgrade database and portable snapshots.
    with job.state.connect() as db:
        db.execute("ALTER TABLE resources DROP COLUMN stage_attempts_json")
    return job, row["key"], old_hash


def test_upgrade_preserves_decisions_and_rebuilds_without_new_requests(
    legacy_task, tmp_path, upgrade_module
):
    job, key, old_hash = legacy_task
    before = (job.root / "videos.jsonl").read_bytes()
    batches = jobs.read(job.root)["filter_batches"]
    backup = tmp_path / "backup"
    result = upgrade_module.upgrade(job.root, backup)
    assert result["filter_fingerprints_upgraded"] == 1
    assert (job.root / "videos.jsonl").read_bytes() == before
    value = job.state.text_filters()[key]
    assert value["original_config_hash"] == old_hash
    assert value["config_hash"] != old_hash
    assert jobs.read(job.root)["filter_batches"] == batches
    with sqlite3.connect(backup / "state.sqlite") as db:
        assert "stage_attempts_json" not in {
            r[1] for r in db.execute("PRAGMA table_info(resources)")
        }
    assert upgrade_module.upgrade(job.root, backup)["status"] == "already_upgraded"
    shutil.rmtree(job.root / ".state")
    resumed = jobs.Job(*jobs.load(job.root), root=job.root)
    with resumed.exclusive():
        assert json.loads(resumed.state.get(key)["stage_attempts_json"]) == {}
        assert filtering.run(resumed)["cached"] == 1


def test_upgrade_does_not_revalidate_stale_source(legacy_task, tmp_path, upgrade_module):
    job, key, old_hash = legacy_task
    with job.state.connect() as db:
        data = json.loads(
            db.execute("SELECT metadata_json FROM resources WHERE key=?", (key,)).fetchone()[0]
        )
        data["title"] = "changed input"
        db.execute("UPDATE resources SET metadata_json=? WHERE key=?", (json.dumps(data), key))
    result = upgrade_module.upgrade(job.root, tmp_path / "backup")
    assert result["incompatible_filters_untouched"] == 1
    assert result["filter_fingerprints_upgraded"] == 0
    assert job.state.text_filters()[key]["config_hash"] == old_hash


def test_upgrade_rolls_back_schema_database_and_pair_on_export_failure(
    legacy_task, tmp_path, monkeypatch, upgrade_module
):
    job, key, old_hash = legacy_task
    before = {name: (job.root / name).read_bytes() for name in ("task.json", "videos.jsonl")}
    real_export = export.write_stage

    def fail(*args, **kwargs):
        real_export(*args, **kwargs)
        raise OSError("simulated export failure")

    monkeypatch.setattr(export, "write_stage", fail)
    with pytest.raises(OSError, match="simulated"):
        upgrade_module.upgrade(job.root, tmp_path / "backup")
    assert before == {name: (job.root / name).read_bytes() for name in before}
    assert job.state.text_filters()[key]["config_hash"] == old_hash
    with job.state.connect() as db:
        assert "stage_attempts_json" not in {
            r[1] for r in db.execute("PRAGMA table_info(resources)")
        }


@pytest.fixture
def old_namespace_task(legacy_task):
    job, key, old_hash = legacy_task
    from video_curator.media import sha256

    path = job.root / "videos.jsonl"
    records = [json.loads(line) for line in path.open()]
    for record in records:
        record["schema"] = "video_collect.video.v3"
    export._atomic_write(records, path)
    task = jobs.read(job.root)
    task["schema"] = "video_collect.task.v2"
    task["videos_sha256"] = sha256(path)
    task["stages"]["upgrade"] = {"version": "stage-retries-and-filter-cache-v1"}
    export.write_json(job.root / "task.json", task)
    return job, records


def test_namespace_upgrade_preserves_video_data(old_namespace_task, tmp_path, upgrade_module):
    job, before = old_namespace_task
    result = upgrade_module.upgrade(job.root, tmp_path / "backup")
    assert result["namespace_upgraded"] is True
    after = [json.loads(line) for line in (job.root / "videos.jsonl").open()]
    assert after == [{**r, "schema": "video_curator.video.v3"} for r in before]
    assert jobs.read(job.root)["schema"] == "video_curator.task.v2"
    assert upgrade_module.upgrade(job.root, tmp_path / "backup")["status"] == "already_upgraded"


def test_namespace_upgrade_failure_restores_old_pair(
    old_namespace_task, tmp_path, upgrade_module, monkeypatch
):
    job, _ = old_namespace_task
    before = {name: (job.root / name).read_bytes() for name in ("task.json", "videos.jsonl")}

    def fail(*args, **kwargs):
        raise OSError("simulated namespace failure")

    monkeypatch.setattr(export, "snapshot", fail)
    with pytest.raises(OSError, match="namespace failure"):
        upgrade_module.upgrade(job.root, tmp_path / "backup")
    assert before == {name: (job.root / name).read_bytes() for name in before}
