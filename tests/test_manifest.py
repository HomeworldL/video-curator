import json
import shutil
from concurrent.futures import ThreadPoolExecutor

import pytest

from video_curator import download, export, jobs, metadata, search
from video_curator.cli import main
from video_curator.identity import identify
from video_curator.jobs import Job
from video_curator.models import (
    Candidate,
    CollectionError,
    SearchBatch,
    SearchRequest,
    VideoMetadata,
)


def records(root):
    with (root / "videos.jsonl").open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def test_repeat_hits_update_provenance_without_losing_metadata_or_download(collection, monkeypatch):
    job, details, calls = collection

    def native(request, runtime):
        return SearchBatch(
            request=request,
            candidates=[
                Candidate(
                    platform="youtube", url=url, query=request.query, mode="native", position=str(i)
                )
                for i, url in enumerate([details.access_url, "https://youtu.be/BaW_jenozKc"])
            ],
            pagination="limit",
            stop_reason="limit_reached",
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", native)
    request = SearchRequest(platform="youtube", query="robot")
    with job.exclusive():
        assert search.discover_and_save(job, request)["new"] == 1
        metadata.run(job, 10**9, missing_only=True)
        download.run(job, 10)
    before = records(job.root)[0]
    with job.exclusive():
        result = search.discover_and_save(job, request.model_copy(update={"query": "机器人"}))
        assert result["new"] == 0 and result["duplicates"] == 2
        export.write_stage(job.state, job.root, "search", result)
    after = records(job.root)[0]
    assert len(records(job.root)) == 1
    assert after["record_id"] == before["record_id"]
    assert len(after["search"]["data"]) == 4
    assert after["metadata"] == before["metadata"]
    assert after["download"] == before["download"]
    assert (
        jobs.read(job.root)["resume_records"][after["record_id"]]["processing"]["status"]
        == "downloaded"
    )
    assert calls == {"metadata": 1, "download": 1, "screen": 0}


def test_canonical_merge_preserves_download_and_all_old_record_ids(collection, monkeypatch):
    job, _, calls = collection
    bv = "https://www.bilibili.com/video/BV17x411w7KC"
    av = "https://www.bilibili.com/video/av170001"
    details = VideoMetadata(identity=identify(bv), access_url=bv, title="robot", duration_seconds=2)
    monkeypatch.setattr("video_curator.metadata.fetch", lambda *args: details)
    with job.exclusive():
        search.import_urls(job, [bv])
        metadata.run(job, 10**9, missing_only=True)
        download.run(job, 1)
    original = records(job.root)[0]
    with job.exclusive():
        search.import_urls(job, [av])
        old_id = job.state.get(identify(av).key)["record_id"]
        metadata.run(job, 10, missing_only=True)
    merged = records(job.root)[0]
    assert len(records(job.root)) == 1
    assert merged["record_id"] == original["record_id"]
    assert jobs.read(job.root)["resume_records"][merged["record_id"]]["merged_record_ids"] == [
        old_id
    ]
    assert len(merged["search"]["data"]) == 2
    assert merged["download"] == original["download"]
    assert (
        jobs.read(job.root)["resume_records"][merged["record_id"]]["processing"]["status"]
        == "downloaded"
    )
    shutil.rmtree(job.root / ".state")
    restarted = Job(*jobs.load(job.root), root=job.root)
    with restarted.exclusive():
        assert search.import_urls(restarted, [av, bv]) == {"new": 0, "duplicates": 2}
        metadata.run(restarted, 10**9, missing_only=True)
        assert download.run(restarted, 10)["processed"] == 0
    assert jobs.read(job.root)["resume_records"][records(job.root)[0]["record_id"]][
        "merged_record_ids"
    ] == [old_id]
    assert calls["download"] == 1


def test_unresolved_identity_keeps_record_id_after_resolution(collection, monkeypatch):
    job, details, _ = collection
    short = "https://b23.tv/test"
    bv = "https://www.bilibili.com/video/BV17x411w7KC"
    resolved = VideoMetadata(identity=identify(bv), access_url=bv)
    monkeypatch.setattr("video_curator.metadata.fetch", lambda *args: resolved)
    search.import_urls(job, [short])
    rid = job.state.get(identify(short).key)["record_id"]
    with job.exclusive():
        metadata.run(job, 1, missing_only=True)
    record = records(job.root)[0]
    assert record["record_id"] == rid
    assert record["identity"]["content_id"] == resolved.identity.content_id
    assert short in record["identity"]["aliases"] and bv in record["identity"]["aliases"]


def test_concurrent_duplicate_inserts_keep_one_resource(collection):
    job, details, _ = collection
    with ThreadPoolExecutor(max_workers=8) as pool:
        added = list(
            pool.map(
                lambda i: job.state.add(
                    Candidate(platform="youtube", url=details.access_url, query=f"query {i}"),
                    details.identity,
                )[1],
                range(40),
            )
        )
    assert sum(added) == 1
    with job.exclusive():
        export.write_stage(job.state, job.root, "search", {})
    assert len(records(job.root)) == 1
    assert len(records(job.root)[0]["search"]["data"]) == 40


def test_portable_search_checkpoints_resume_only_failed_page(collection, monkeypatch, tmp_path):
    job, details, _ = collection
    calls = []

    def native(request, runtime):
        calls.append(request.page)
        if calls == [1, 2]:
            raise CollectionError("rate_limited", "search")
        url = details.access_url if request.page == 1 else "https://youtu.be/abcdefghijk"
        return SearchBatch(
            request=request,
            candidates=[Candidate(platform="youtube", url=url)],
            pagination="page",
            next_page=2 if request.page == 1 else None,
            stop_reason="page_limit" if request.page == 1 else "returned_batch_end",
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", native)
    requests = [SearchRequest(platform="youtube", query="robot")]
    with job.exclusive():
        assert search.run(job, requests, all_pages=True)["status"] == "partial"
    original_id = records(job.root)[0]["record_id"]
    moved = tmp_path / "moved"
    moved.mkdir()
    for name in ("videos.jsonl", "task.json"):
        shutil.copyfile(job.root / name, moved / name)
    restarted = Job(*jobs.load(moved), root=moved)
    with restarted.exclusive():
        assert search.run(restarted, requests, all_pages=True)["status"] == "complete"
    assert calls == [1, 2, 2]
    assert original_id in {r["record_id"] for r in records(moved)}
    assert len(records(moved)) == 2
    assert {p.name for p in moved.iterdir()} == {"videos.jsonl", "task.json", ".state"}


@pytest.mark.parametrize("remove_db", [True, False])
def test_moved_downloaded_task_reuses_originals(collection, tmp_path, remove_db):
    job, details, calls = collection
    with job.exclusive():
        search.import_urls(job, [details.access_url])
        metadata.run(job, 10**9, missing_only=True)
        download.run(job, 1)
    moved = tmp_path / "moved"
    job.root.rename(moved)
    if remove_db:
        shutil.rmtree(moved / ".state")
    restarted = Job(*jobs.load(moved), root=moved)
    with restarted.exclusive():
        metadata.run(restarted, 10**9, missing_only=True)
        assert download.run(restarted, 1)["processed"] == 0
        metadata.run(restarted, 10**9, missing_only=True)
        assert download.run(restarted, 1)["downloaded_records"] == 1
    assert calls == {"metadata": 1, "download": 1, "screen": 0}
    assert records(moved)[0]["download"]["status"] == "complete"


def test_metadata_failure_is_in_record_and_success_clears_it(collection, monkeypatch):
    job, details, _ = collection
    search.import_urls(job, [details.access_url])

    def fail(*args):
        raise CollectionError("needs_login", "metadata")

    monkeypatch.setattr("video_curator.metadata.fetch", fail)
    with job.exclusive(), pytest.raises(CollectionError, match="needs_login"):
        metadata.run(job, 1)
    assert records(job.root)[0]["metadata"]["error"] == "needs_login"
    monkeypatch.setattr("video_curator.metadata.fetch", lambda *args: details)
    with job.exclusive():
        metadata.run(job, 1)
    assert records(job.root)[0]["metadata"]["status"] == "complete"
    assert records(job.root)[0]["metadata"]["error"] is None


def test_csv_export_is_explicit_and_does_not_change_manifest(collection, tmp_path, capsys):
    job, details, _ = collection
    with job.exclusive():
        search.import_urls(job, [details.access_url])
        metadata.run(job, 10**9, missing_only=True)
        download.run(job, 1)
    before = (job.root / "videos.jsonl").read_bytes()
    output = tmp_path / "selected.csv"
    assert (
        main(
            [
                "export",
                "--input",
                str(job.root),
                "--format",
                "csv",
                "--selection",
                "downloaded",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["exported_records"] == 1
    assert output.read_text(encoding="utf-8-sig").count("\n") == 2
    assert (job.root / "videos.jsonl").read_bytes() == before
    assert not (job.root / "index.jsonl").exists()
    assert not (job.root / "downloaded.jsonl").exists()


def test_interrupted_manifest_publication_cannot_skip_search_results(collection, monkeypatch):
    job, details, _ = collection
    calls = []

    def native(request, runtime):
        calls.append(request.page)
        return SearchBatch(
            request=request,
            candidates=[Candidate(platform="youtube", url=details.access_url)],
            pagination="page",
            next_page=None,
            stop_reason="returned_batch_end",
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", native)
    original = export.write_json
    requests = [SearchRequest(platform="youtube", query="robot")]

    def fail_task(path, value):
        if path.name == "task.json" and value.get("videos_sha256"):
            raise OSError("simulated crash after videos.jsonl replacement")
        return original(path, value)

    monkeypatch.setattr(export, "write_json", fail_task)
    with job.exclusive(), pytest.raises(OSError):
        search.run(job, requests)
    assert len(records(job.root)) == 1
    rid = records(job.root)[0]["record_id"]
    monkeypatch.setattr(export, "write_json", original)
    # Existing SQLite repairs a publication interrupted between the two files.
    resumed = Job(*jobs.load(job.root), root=job.root)
    with resumed.exclusive():
        export.write_stage(resumed.state, resumed.root, "export", {})
    shutil.rmtree(job.root / ".state")
    resumed = Job(*jobs.load(job.root), root=job.root)
    with resumed.exclusive():
        assert search.run(resumed, requests)["status"] == "complete"
    assert calls == [1]  # Complete checkpoints survive rebuilding SQLite.
    assert len(records(job.root)) == 1
    assert records(job.root)[0]["record_id"] == rid


def test_only_manifests_do_not_claim_missing_video_already_downloaded(collection, tmp_path):
    job, details, calls = collection
    with job.exclusive():
        search.import_urls(job, [details.access_url])
        metadata.run(job, 10**9, missing_only=True)
        download.run(job, 1)
    moved = tmp_path / "moved"
    moved.mkdir()
    for name in ("videos.jsonl", "task.json"):
        shutil.copyfile(job.root / name, moved / name)
    resumed = Job(*jobs.load(moved), root=moved)
    with resumed.exclusive():
        metadata.run(resumed, 10**9, missing_only=True)
        assert download.run(resumed, 1)["processed"] == 1
    assert calls["download"] == 2
    assert (moved / records(moved)[0]["download"]["data"]["path"]).is_file()


def test_v2_contract_has_uniform_stages_and_lossless_execution_sidecar(collection):
    from video_curator.models import VideoRecord

    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    with job.exclusive():
        export.write_stage(job.state, job.root, "search", {})
    record = records(job.root)[0]
    assert set(record) == {
        "schema",
        "record_id",
        "identity",
        "search",
        "metadata",
        "filter",
        "download",
        "screening",
    }
    assert record["schema"] == "video_curator.video.v3"
    assert VideoRecord.model_validate(record).record_id == record["record_id"]
    for name in ("search", "metadata", "filter", "download", "screening"):
        assert set(record[name]) == {"status", "error", "data"}
    assert record["filter"] == {"status": "not_run", "error": None, "data": None}
    saved = jobs.read(job.root)["resume_records"][record["record_id"]]
    assert saved["candidate"]["url"] == details.access_url
    assert saved["processing"]["status"] == "discovered"
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        download.run(job, 1)
    record = records(job.root)[0]
    assert "identity" not in record["metadata"]["data"]
    assert record["metadata"]["data"]["access_url"] == details.access_url


def test_mixed_manifest_generations_fail_before_rebuilding_state(collection, tmp_path):
    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    with job.exclusive():
        metadata.run(job, 10**9, missing_only=True)
        download.run(job, 1)
    moved = tmp_path / "mismatch"
    shutil.copytree(job.root, moved, ignore=shutil.ignore_patterns(".state"))
    with (moved / "videos.jsonl").open("a") as f:
        f.write("\n")
    restarted = Job(*jobs.load(moved), root=moved)
    with pytest.raises(CollectionError, match="task_snapshot_mismatch"):
        with restarted.exclusive():
            pytest.fail("A mixed snapshot must not issue any requests")
    assert restarted.state.rows() == []


def test_unicode_line_separators_in_metadata_survive_handoff(collection, tmp_path):
    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    text = "第一行\u2028第二行\u2029第三行\x85第四行"
    with job.exclusive():
        job.state.merge_metadata(
            details.identity.key, details.model_copy(update={"description": text})
        )
        export.write_stage(job.state, job.root, "metadata", {})
    moved = tmp_path / "unicode-handoff"
    shutil.copytree(job.root, moved, ignore=shutil.ignore_patterns(".state"))
    restarted = Job(*jobs.load(moved), root=moved)
    with restarted.exclusive():
        assert len(restarted.state.rows()) == 1
        assert json.loads(restarted.state.rows()[0]["metadata_json"])["description"] == text


def test_move_preserves_unexported_state_and_unknown_model_request(collection, tmp_path):
    job, details, _ = collection
    with job.exclusive():
        search.import_urls(job, [details.access_url])
        metadata.run(job, 1, missing_only=True)
        # These commits are newer than videos.jsonl/task.json (e.g. interrupted screen).
        job.state.save_screen(
            details.identity.key,
            {
                "status": "in_flight",
                "error": None,
                "pages": [{"evidence": {"path": str(job.root / "evidence" / "page.jpg")}}],
                "requested_calls": 1,
            },
        )
        search.import_urls(job, ["https://youtu.be/abcdefghijk"])
    moved = tmp_path / "moved-live-state"
    job.root.rename(moved)
    # Even an interrupted snapshot export must not replace a complete database.
    (moved / "videos.jsonl").write_text("incomplete export")
    restarted = Job(*jobs.load(moved), root=moved)
    with restarted.exclusive():
        assert len(restarted.state.rows()) == 2
        value = restarted.state.screens()[details.identity.key]
        assert value["status"] == "failed"
        assert value["error"] == "outcome_unknown"
        assert value["requested_calls"] == 1
        assert value["pages"][0]["evidence"]["path"] == str(moved / "evidence" / "page.jpg")
        assert restarted.state.setting("root") == str(moved)


def test_stage_retry_budgets_survive_snapshot_restore(collection):
    job, details, calls = collection
    key = details.identity.key
    job.topic.download.max_attempts = 1
    with job.exclusive():
        search.import_urls(job, [details.access_url])
        for _ in range(2):
            job.state.update(key, "technical_error", "timeout", attempt=True, stage="metadata")
        metadata.run(job, 1, missing_only=True)
    shutil.rmtree(job.root / ".state")
    restarted = Job(*jobs.load(job.root), root=job.root)
    restarted.topic.download.max_attempts = 1
    with restarted.exclusive():
        assert json.loads(restarted.state.get(key)["stage_attempts_json"]) == {"metadata": 2}
        assert download.run(restarted, 1)["processed"] == 1
    assert calls["download"] == 1


def test_move_path_rebase_rolls_back_on_invalid_asset(collection, tmp_path):
    job, details, _ = collection
    with job.exclusive():
        search.import_urls(job, [details.access_url])
        metadata.run(job, 1, missing_only=True)
        download.run(job, 1)
        old_media = job.state.artifact(details.identity.key)["media_json"]
        job.state.save_screen(
            details.identity.key,
            {
                "status": "pending",
                "pages": [{"evidence": {"path": "/outside/page.jpg"}}],
            },
        )
    old_root = job.root
    moved = tmp_path / "invalid-path"
    old_root.rename(moved)
    restarted = Job(*jobs.load(moved), root=moved)
    with pytest.raises(CollectionError, match="task_asset_outside_directory"):
        with restarted.exclusive():
            pass
    assert restarted.state.setting("root") == str(old_root)
    assert restarted.state.artifact(details.identity.key)["media_json"] == old_media
