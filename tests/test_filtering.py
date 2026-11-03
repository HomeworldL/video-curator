import copy
import json
import shutil

import httpx
import pytest
import yaml

from video_curator import download, export, filtering, jobs, metadata, search
from video_curator.cli import main
from video_curator.config import Runtime, Topic
from video_curator.identity import identify
from video_curator.jobs import Job
from video_curator.models import Candidate, CollectionError, VideoMetadata
from video_curator.providers import doubao


def envelope(results):
    return {
        "id": "test-request",
        "usage": {"prompt_tokens": 80, "completion_tokens": 30},
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps({"results": results})}}
        ],
    }


def answer(items, decision="keep"):
    items = filtering.wire_items(items) if items and "record_id" in items[0] else items
    return envelope(
        [
            {
                "id": i["id"],
                "decision": decision,
                "reason": i["title"],
            }
            for i in reversed(items)
        ]
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ARK_API_KEY", "test-not-sent")
    monkeypatch.delenv("ARK_FILTER_MODEL", raising=False)
    job = Job(
        Topic(id="test", filter={"prompt": "Keep relevant subjects; reject unboxing."}),
        Runtime(dataset_root=tmp_path),
    )
    calls = []

    def transport(system, items, settings, model):
        calls.append(copy.deepcopy(items))
        return answer(items)

    monkeypatch.setattr(doubao, "complete_text", transport)

    def add(count):
        for n in range(count):
            url = f"https://www.youtube.com/watch?v={n:011d}"
            identity = identify(url)
            key, _ = job.state.add(Candidate(platform="youtube", url=url), identity)
            # Stable fixture IDs make scheduling deterministic.
            with job.state.connect() as db:
                db.execute("UPDATE resources SET record_id=? WHERE key=?", (f"{n:032d}", key))
            job.state.merge_metadata(
                key,
                VideoMetadata(
                    identity=identity,
                    access_url=url,
                    title=f"video {n}",
                    description="description",
                    tags=["tagA", "tagB"],
                ),
            )
        return job.state.rows()

    return job, calls, add


def test_fifty_batch_id_mapping_cache_and_portable_restore(setup, tmp_path):
    job, calls, add = setup
    add(51)
    with job.exclusive():
        result = filtering.run(job)
    assert [len(c) for c in calls] == [50, 1]
    assert result["processed"] == 51
    assert result["usage"] == {"prompt_tokens": 160, "completion_tokens": 60}
    records, audit = export.snapshot(job.state, job.root)
    assert len(audit["filter_batches"]) == 2
    for r in records:
        assert (
            audit["resume_records"][r["record_id"]]["filter"]["result"]["record_id"]
            == r["record_id"]
        )
        assert r["filter"]["data"]["reason"] == r["metadata"]["data"]["title"]
        assert r["screening"]["status"] == "not_run"
    job.topic.filter.batch_size = 10
    with job.exclusive():
        assert filtering.run(job)["calls"] == 0
    moved = tmp_path / "moved"
    shutil.copytree(job.root, moved, ignore=shutil.ignore_patterns(".state"))
    rebuilt = Job(job.topic, job.runtime, root=moved)
    with rebuilt.exclusive():
        assert filtering.run(rebuilt)["cached"] == 51
    assert len(calls) == 2
    assert len(jobs.read(moved)["filter_batches"]) == 2
    assert {p.name for p in moved.iterdir()} == {".state", "task.json", "videos.jsonl"}


def test_batch_size_limit_and_prompt_change(setup):
    job, calls, add = setup
    add(23)
    job.topic.filter.batch_size = 10
    with job.exclusive():
        assert filtering.run(job, limit=21)["processed"] == 21
        assert [len(c) for c in calls] == [10, 10, 1]
        assert filtering.run(job)["processed"] == 2
        job.topic.filter.prompt += " revised"
        assert filtering.run(job, limit=1)["processed"] == 1
    values = job.state.text_filters().values()
    assert sum(v["status"] == "stale" for v in values) == 22
    assert all(v["decision"] is None for v in values if v["status"] == "stale")


ITEMS = [
    {"record_id": "a", "title": "first", "description": "one", "tags": ["tagA", "tagB"]},
    {"record_id": "b", "title": "second", "description": "two", "tags": []},
]


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "duplicate",
        "unknown",
        "wrong_type",
        "truncated",
        "no_finish",
        "tool_calls",
        "duplicate_json_key",
        "markdown",
        "empty",
        "nan",
        "extra_choice",
    ],
)
def test_unreliable_batch_is_error_without_guessing_ids(bad):
    payload = answer(ITEMS)
    results = json.loads(payload["choices"][0]["message"]["content"])["results"]
    if bad == "missing":
        results.pop()
    elif bad == "duplicate":
        results[0] = results[1]
    elif bad == "unknown":
        results[0]["id"] = 99
    elif bad == "wrong_type":
        results[0]["id"] = "1"
    payload = envelope(results)
    choice = payload["choices"][0]
    if bad == "truncated":
        choice["finish_reason"] = "length"
    elif bad == "no_finish":
        del choice["finish_reason"]
    elif bad == "tool_calls":
        choice["message"]["tool_calls"] = [{}]
    elif bad == "duplicate_json_key":
        choice["message"]["content"] = '{"results":[],"results":' + json.dumps(results) + "}"
    elif bad == "markdown":
        choice["message"]["content"] = "```json\n" + choice["message"]["content"] + "\n```"
    elif bad == "empty":
        choice["message"]["content"] = ""
    elif bad == "nan":
        choice["message"]["content"] = '{"results":NaN}'
    elif bad == "extra_choice":
        payload["choices"].append(copy.deepcopy(choice))
    results = filtering.validate(payload, ITEMS)
    assert set(results) == {"a", "b"}
    assert all(
        r["decision"] == "unclassified" and r["error"] == "invalid_batch_response"
        for r in results.values()
    )


@pytest.mark.parametrize(
    "bad", ["enum", "model_error", "blank_reason", "missing_reason", "wrong_reason_type"]
)
def test_one_bad_record_does_not_discard_good_records(bad):
    records = json.loads(answer(ITEMS)["choices"][0]["message"]["content"])["results"]
    if bad == "enum":
        records[0]["decision"] = "accept"
    elif bad == "model_error":
        records[0]["decision"] = "unclassified"
    elif bad == "blank_reason":
        records[0]["reason"] = "  "
    elif bad == "missing_reason":
        del records[0]["reason"]
    elif bad == "wrong_reason_type":
        records[0]["reason"] = ["text"]
    results = filtering.validate(envelope(records), ITEMS)
    assert results["a"]["decision"] == "keep"
    assert results["b"]["decision"] == "unclassified"
    assert results["b"]["error"] == "invalid_record_fields"


def test_classifier_needs_no_evidence_and_ignores_extra_explanation():
    records = json.loads(answer(ITEMS)["choices"][0]["message"]["content"])["results"]
    records[0].update(decision="uncertain", evidence=[{"field": "tags", "quote": "not in input"}])
    results = filtering.validate(envelope(records), ITEMS)
    assert results["a"]["decision"] == "keep"
    assert results["b"]["decision"] == "uncertain"
    assert "evidence" not in results["b"]


def test_invalid_batch_is_recorded_and_next_batch_continues_without_retry(setup, monkeypatch):
    job, calls, add = setup
    add(52)

    def broken(system, items, settings, model):
        calls.append(items)
        response = answer(items, "reject")
        response["choices"][0]["finish_reason"] = "length"
        return response

    monkeypatch.setattr(doubao, "complete_text", broken)
    with job.exclusive():
        summary = filtering.run(job)
        assert summary["calls"] == 2 and summary["processed"] == 52
        assert summary["stopped"] is None and summary["outcomes"] == {"unclassified": 52}
        assert summary["usage"]["prompt_tokens"] == 160
        assert all(v["decision"] == "unclassified" for v in job.state.text_filters().values())
        assert filtering.run(job)["calls"] == 0
        assert filtering.run(job)["error_records_skipped"] == 52
        monkeypatch.setattr(doubao, "complete_text", lambda s, i, c, m: answer(i))
        assert filtering.run(job, retry_failed=True)["processed"] == 52


@pytest.mark.parametrize(
    "error", [CollectionError("outcome_unknown", "filter"), KeyboardInterrupt()]
)
def test_timeout_and_interrupt_preserve_unknown_and_do_not_repeat(setup, monkeypatch, error):
    job, calls, add = setup
    add(2)

    def fail(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(doubao, "complete_text", fail)
    with job.exclusive():
        if isinstance(error, KeyboardInterrupt):
            with pytest.raises(CollectionError, match="interrupted"):
                filtering.run(job)
        else:
            assert filtering.run(job)["stopped"] == "outcome_unknown"
    with job.exclusive():
        assert filtering.run(job)["calls"] == 0
    assert len(calls) == 1
    assert all(v["status"] == "outcome_unknown" for v in job.state.text_filters().values())


def test_crash_recovery_and_stale_source_never_excludes(setup):
    job, calls, add = setup
    rows = add(1)
    with job.exclusive():
        filtering.run(job)
    values = job.state.text_filters()
    value = values[rows[0]["key"]]
    value.update(status="in_flight", decision=None)
    job.state.save_text_filters(values)
    with job.exclusive():
        assert filtering.run(job)["calls"] == 0
    assert job.state.text_filters()[rows[0]["key"]]["status"] == "outcome_unknown"
    value.update(status="completed", decision="reject", source_hash="changed")
    job.state.save_text_filters(values)
    assert len(job.state.rows(exclude_filtered=True)) == 1
    assert export.snapshot(job.state, job.root)[0][0]["filter"]["status"] == "stale"


def test_download_limit_applied_after_reject_exclusion(setup, monkeypatch):
    job, calls, add = setup
    rows = add(3)

    def mixed(s, items, c, m):
        results = json.loads(answer(items)["choices"][0]["message"]["content"])["results"]
        for r in results:
            r["decision"] = "reject" if r["id"] == 1 else "uncertain"
        return envelope(results)

    monkeypatch.setattr(doubao, "complete_text", mixed)
    with job.exclusive():
        filtering.run(job)
        assert metadata.run(job, 1, exclude_filtered=True) == [rows[1]["key"]]
    assert len(job.state.rows()) == 3


def test_text_length_split_and_oversize_not_sent(setup):
    job, calls, add = setup
    rows = add(3)
    system = filtering.system_prompt(job.topic.filter.prompt)
    items = [filtering.source(r) for r in rows]
    job.topic.filter.max_batch_chars = len(system) + len(
        json.dumps({"videos": filtering.wire_items(items[:1])}, ensure_ascii=False)
    )
    with job.exclusive():
        filtering.run(job)
    assert [len(c) for c in calls] == [1, 1, 1]
    job.topic.filter.prompt += "x" * 100
    with job.exclusive():
        assert filtering.run(job)["stopped"] == "filter_input_too_large"
    assert len(calls) == 3


def test_cli_new_prompt_for_old_snapshot_and_saved_filter_config(setup, tmp_path, capsys):
    job, calls, add = setup
    add(12)
    with job.exclusive():
        export.write_stage(job.state, job.root, "metadata", {})
    topic = tmp_path / "topic.yaml"
    topic.write_text(
        yaml.safe_dump({"id": "test", "filter": {"prompt": "new general prompt", "batch_size": 10}})
    )
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("timeout_seconds: 30\n")
    args = ["filter", "--input", str(job.root)]
    assert main(args + ["--topic", str(topic), "--runtime", str(runtime), "--limit", "10"]) == 0
    assert main(args) == 0
    assert [len(c) for c in calls] == [10, 2]
    task = jobs.read(job.root)
    assert "filter_config" not in task
    assert task["topic"]["filter"]["prompt"] == "new general prompt"
    capsys.readouterr()


def test_provider_text_only_no_thinking_no_tools(monkeypatch):
    from video_curator.config import FilterSettings

    captured = []
    monkeypatch.setenv("ARK_API_KEY", "fake")

    def post(url, **kwargs):
        captured.append(kwargs["json"])
        return httpx.Response(200, json=answer(ITEMS))

    monkeypatch.setattr(httpx, "post", post)
    payload = doubao.complete_text("prompt", ITEMS, FilterSettings(), "test-model")
    assert payload["usage"]["prompt_tokens"] == 80
    body = captured[0]
    assert body["thinking"] == {"type": "disabled"}
    assert "tools" not in body
    assert body["response_format"] == {"type": "json_object"}
    assert json.loads(body["messages"][1]["content"])["videos"] == ITEMS


def test_max_calls_resume_and_missing_metadata_not_sent(setup):
    job, calls, add = setup
    add(3)
    search.import_urls(job, ["https://www.youtube.com/watch?v=abcdef12345"])
    job.topic.filter.batch_size = 2
    job.topic.filter.max_calls = 1
    with job.exclusive():
        result = filtering.run(job)
        assert result["stopped"] == "max_calls_reached"
        assert result["missing_metadata"] == 1
        assert filtering.run(job)["processed"] == 1
    assert [len(c) for c in calls] == [2, 1]


def test_filter_identity_merge_is_safe(setup):
    job, calls, add = setup
    rows = add(2)
    with job.exclusive():
        filtering.run(job)
        target = VideoMetadata.model_validate_json(rows[1]["metadata_json"])
        job.state.merge_metadata(rows[0]["key"], target)
        export.write_stage(job.state, job.root, "metadata", {})
    assert len(job.state.rows()) == 1
    records, audit = export.snapshot(job.state, job.root)
    assert (
        audit["resume_records"][records[0]["record_id"]]["filter"]["result"]["record_id"]
        == rows[1]["record_id"]
    )
    assert len(audit["filter_batches"]) == 1


def test_actual_download_skips_text_reject_and_preserves_filter(collection, monkeypatch):
    job, details, calls = collection
    job.topic.filter.prompt = "Keep relevant subjects."
    search.import_urls(job, [details.access_url])
    with job.exclusive():
        metadata.run(job, 10)
        monkeypatch.setattr(doubao, "complete_text", lambda s, i, c, m: answer(i, "reject"))
        filtering.run(job)
        assert download.run(job, 1)["processed"] == 0
    assert calls["download"] == 0
    record = export.snapshot(job.state, job.root)[0][0]
    assert record["filter"]["data"]["decision"] == "reject"
    assert record["screening"]["status"] == "not_run"
    assert record["metadata"]["data"]["title"] == details.title


def test_wire_uses_short_ids_restarts_each_batch_and_persists_mapping(setup):
    job, calls, add = setup
    rows = add(3)
    job.topic.filter.batch_size = 2
    with job.exclusive():
        filtering.run(job)
    assert [[i["id"] for i in batch] for batch in calls] == [[1, 2], [1]]
    assert all("record_id" not in item for batch in calls for item in batch)
    _, audit = export.snapshot(job.state, job.root)
    for batch in audit["filter_batches"]:
        assert batch["id_map"] == {
            str(i): entry["source"]["record_id"] for i, entry in enumerate(batch["entries"], 1)
        }
    assert {v["result"]["record_id"] for v in job.state.text_filters().values()} == {
        r["record_id"] for r in rows
    }


@pytest.mark.parametrize("bad_id", [True, 1.0, 0, -1, "2", 201])
def test_short_ids_are_strict_integers(bad_id):
    results = json.loads(answer(ITEMS)["choices"][0]["message"]["content"])["results"]
    results[0]["id"] = bad_id
    assert all(
        r["decision"] == "unclassified"
        for r in filtering.validate(envelope(results), ITEMS).values()
    )


def test_filter_publishes_once_after_multiple_batches(setup, monkeypatch):
    job, calls, add = setup
    add(5)
    job.topic.filter.batch_size = 2
    published = []
    original = export._atomic_write

    def write(records, path):
        if path.name == "videos.jsonl":
            published.append(len(calls))
        return original(records, path)

    monkeypatch.setattr(export, "_atomic_write", write)
    with job.exclusive():
        filtering.run(job)
    assert published == [3]
    assert json.loads(job.state.setting("filter_progress"))["processed"] == 5


class SimulatedCrash(BaseException):
    pass


def crash_after_first_batch(job, monkeypatch):
    job.topic.filter.batch_size = 2
    called = []

    def crash(system, items, settings, model):
        called.append(items)
        if len(called) == 2:
            raise SimulatedCrash()
        return answer(items)

    monkeypatch.setattr(doubao, "complete_text", crash)
    with job.exclusive():
        export.write_stage(job.state, job.root, "metadata", {})
        before = (job.root / "videos.jsonl").read_bytes()
        with pytest.raises(SimulatedCrash):
            filtering.run(job)
    assert (job.root / "videos.jsonl").read_bytes() == before
    assert sum(v["status"] == "completed" for v in job.state.text_filters().values()) == 2


def test_crash_without_export_recovers_completed_and_unknown_from_sqlite(
    setup, monkeypatch, tmp_path
):
    job, calls, add = setup
    add(5)
    crash_after_first_batch(job, monkeypatch)
    restarted = Job(job.topic, job.runtime, root=job.root)
    calls_after_restart = []

    def complete(system, items, settings, model):
        calls_after_restart.append(items)
        return answer(items)

    monkeypatch.setattr(doubao, "complete_text", complete)
    with restarted.exclusive():
        summary = filtering.run(restarted)
    assert summary["cached"] == 2
    assert summary["error_records_skipped"] == 2
    assert summary["processed"] == 1
    assert [len(c) for c in calls_after_restart] == [1]
    moved = tmp_path / "handoff-after-recovery"
    shutil.copytree(job.root, moved, ignore=shutil.ignore_patterns(".state"))
    portable = Job(job.topic, job.runtime, root=moved)
    with portable.exclusive():
        summary = filtering.run(portable)
    assert (
        summary["cached"] == 3 and summary["error_records_skipped"] == 2 and summary["calls"] == 0
    )


def test_explicit_export_refreshes_two_files_from_sqlite_after_crash(setup, monkeypatch, capsys):
    job, calls, add = setup
    add(4)
    crash_after_first_batch(job, monkeypatch)
    assert main(["export", "--input", str(job.root)]) == 0
    rows = [json.loads(line) for line in (job.root / "videos.jsonl").read_text().splitlines()]
    assert sum(r["filter"]["status"] == "complete" for r in rows) == 2
    assert sum(r["filter"]["status"] == "failed" for r in rows) == 2
    assert jobs.read(job.root)["stages"]["filter"]["status"] == "interrupted"
    assert not (job.root / "exports").exists()
    capsys.readouterr()


def test_old_protocol_results_and_unknown_requests_survive_upgrade(setup):
    job, calls, add = setup
    rows = add(2)
    descriptor = filtering.descriptor(
        job.topic.filter.prompt, job.topic.filter, job.topic.filter.model
    )
    descriptor.pop("prompt")
    descriptor.update(
        harness_version=1,
        system=job.topic.filter.prompt + "\n\n以下是批量文本审核协议。旧的完整 ID 协议",
    )
    config_hash = filtering.digest(descriptor)
    values = {}
    for number, row in enumerate(rows):
        item = filtering.source(row)
        values[row["key"]] = {
            "status": "completed" if number == 0 else "outcome_unknown",
            "decision": "keep" if number == 0 else None,
            "result": {
                "record_id": row["record_id"],
                "decision": "keep",
                "reason": "subject relevant",
                "evidence": [{"field": "title", "quote": item["title"]}],
            }
            if number == 0
            else None,
            "error": None if number == 0 else "outcome_unknown",
            "input_hash": filtering.input_hash(item, config_hash),
            "source_hash": filtering.digest(item),
            "config_hash": config_hash,
            "batch_id": "old",
            "updated_at": "2026-10-29T00:00:00+00:00",
        }
    job.state.save_text_filters(
        values, {"id": "old", "status": "completed", "descriptor": descriptor}
    )
    with job.exclusive():
        summary = filtering.run(job)
    assert summary["cached"] == 1 and summary["error_records_skipped"] == 1
    assert calls == []
    expected = copy.deepcopy(values)
    expected[rows[0]["key"]]["result"].pop("evidence")
    expected[rows[1]["key"]].update(
        decision="unclassified",
        result={
            "record_id": rows[1]["record_id"],
            "decision": "unclassified",
            "reason": "outcome_unknown",
        },
    )
    assert job.state.text_filters() == expected
    job.topic.filter.prompt += " changed policy"
    with job.exclusive():
        assert filtering.run(job)["processed"] == 2
    assert len(calls) == 1


def test_two_concurrent_batches_bound_workers_and_map_out_of_order(setup, monkeypatch):
    import threading

    job, calls, add = setup
    add(5)
    job.topic.filter.batch_size = 1
    job.topic.filter.workers = 2
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    running = peak = 0
    writer_threads = []
    original_save = job.state.save_text_filters

    def save(*args, **kwargs):
        writer_threads.append(threading.get_ident())
        return original_save(*args, **kwargs)

    def complete(system, items, settings, model):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            calls.append(items)
        if items[0]["title"] in ("video 0", "video 1"):
            barrier.wait(timeout=3)
        with lock:
            running -= 1
        return answer(items)

    monkeypatch.setattr(doubao, "complete_text", complete)
    monkeypatch.setattr(job.state, "save_text_filters", save)
    with job.exclusive():
        summary = filtering.run(job)
        job.topic.filter.workers = 1
        assert filtering.run(job)["calls"] == 0
    assert summary["processed"] == 5 and summary["calls"] == 5
    assert peak == 2
    assert set(writer_threads) == {threading.get_ident()}
    records, _ = export.snapshot(job.state, job.root)
    for record in records:
        assert record["filter"]["data"]["reason"] == record["metadata"]["data"]["title"]


def test_concurrent_failure_stops_refill_and_saves_other_inflight_batch(setup, monkeypatch):
    import threading

    job, calls, add = setup
    add(6)
    job.topic.filter.batch_size = 1
    job.topic.filter.workers = 2
    barrier = threading.Barrier(2)
    failure_saved = threading.Event()
    original_save = job.state.save_text_filters

    def save(values, batch=None, **kwargs):
        result = original_save(values, batch, **kwargs)
        if batch and batch["status"] == "failed":
            failure_saved.set()
        return result

    def complete(system, items, settings, model):
        calls.append(items)
        barrier.wait(timeout=3)
        if items[0]["title"] == "video 0":
            raise CollectionError("rate_limited", "filter")
        assert failure_saved.wait(timeout=3)
        return answer(items)

    monkeypatch.setattr(doubao, "complete_text", complete)
    monkeypatch.setattr(job.state, "save_text_filters", save)
    with job.exclusive():
        summary = filtering.run(job)
    assert len(calls) == 2
    assert summary["stopped"] == "rate_limited"
    assert summary["processed"] == 2
    assert summary["outcomes"] == {"keep": 1, "unclassified": 1}
    assert sorted(v["status"] for v in job.state.text_filters().values()) == [
        "completed",
        "failed",
    ]


def test_concurrent_budget_is_global(setup):
    job, calls, add = setup
    add(5)
    job.topic.filter.batch_size = 1
    job.topic.filter.workers = 2
    job.topic.filter.max_calls = 1
    with job.exclusive():
        summary = filtering.run(job)
    assert summary["calls"] == 1 and summary["processed"] == 1
    assert summary["stopped"] == "max_calls_reached" and len(calls) == 1


def test_cli_concurrency_overrides_saved_settings(setup, capsys):
    job, calls, add = setup
    add(1)
    with job.exclusive():
        export.write_stage(job.state, job.root, "metadata", {})
    args = ["filter", "--input", str(job.root)]
    assert main(args + ["--workers", "2"]) == 0
    assert jobs.read(job.root)["topic"]["filter"]["workers"] == 2
    assert main(args + ["--workers", "1"]) == 0
    assert jobs.read(job.root)["topic"]["filter"]["workers"] == 1
    assert len(calls) == 1
    capsys.readouterr()


def test_ctrl_c_stops_scheduling_but_drains_inflight_requests(setup, monkeypatch):
    import threading

    from video_curator import filtering as filter_module

    job, calls, add = setup
    add(6)
    job.topic.filter.batch_size = 1
    job.topic.filter.workers = 2
    release = threading.Event()
    original_wait = filter_module.wait
    interrupted = False

    def complete(system, items, settings, model):
        calls.append(items)
        assert release.wait(timeout=3)
        return answer(items)

    def interrupt_once(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            release.set()
            raise KeyboardInterrupt()
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(doubao, "complete_text", complete)
    monkeypatch.setattr(filter_module, "wait", interrupt_once)
    with job.exclusive():
        with pytest.raises(CollectionError, match="interrupted"):
            filtering.run(job)
    assert len(calls) == 2
    assert all(v["status"] == "completed" for v in job.state.text_filters().values())
    assert jobs.read(job.root)["stages"]["filter"]["processed"] == 2


def test_mixed_record_errors_persist_and_do_not_block_download_or_next_batch(
    setup, monkeypatch, tmp_path
):
    job, calls, add = setup
    add(3)
    job.topic.filter.batch_size = 2

    def mixed(system, items, settings, model):
        calls.append(items)
        payload = answer(items)
        records = json.loads(payload["choices"][0]["message"]["content"])["results"]
        if len(items) == 2:
            records[0]["decision"] = "not-a-category"
            records[1]["decision"] = "reject"
        return envelope(records)

    monkeypatch.setattr(doubao, "complete_text", mixed)
    with job.exclusive():
        summary = filtering.run(job)
    assert summary["outcomes"] == {"unclassified": 1, "reject": 1, "keep": 1}
    assert summary["stopped"] is None and summary["processed"] == 3
    assert len(calls) == 2
    assert len(job.state.rows(exclude_filtered=True)) == 2
    records, audit = export.snapshot(job.state, job.root)
    assert audit["filter_batches"][0]["status"] == "completed_with_errors"
    assert (
        audit["filter_batches"][0]["response"]["choices"][0]["message"]["content"].find(
            "not-a-category"
        )
        >= 0
    )
    assert sum(r["filter"]["data"]["decision"] == "unclassified" for r in records) == 1
    moved = tmp_path / "errors-handoff"
    shutil.copytree(job.root, moved, ignore=shutil.ignore_patterns(".state"))
    portable = Job(job.topic, job.runtime, root=moved)
    with portable.exclusive():
        resumed = filtering.run(portable)
    assert (
        resumed["calls"] == 0 and resumed["cached"] == 2 and resumed["error_records_skipped"] == 1
    )


def test_removing_only_evidence_output_instructions_reuses_old_policy(setup):
    job, _, _ = setup
    criteria = "Keep relevant content.\nreject 必须有支持明确排除条件的直接文字证据；证据不足时改为 keep 或 uncertain。\n"
    old_prompt = (
        criteria
        + "按调用方提供的 JSON Schema 输出 decision（keep/reject/uncertain）、reason 和 evidence。\n"
        + "evidence 引用标题、简介或标签中的短原文。每项只引用一个字段的一段连续原文；标签逐个引用，不拼接、不改写。\n"
    )
    new_prompt = (
        criteria
        + "按调用方提供的 JSON Schema 输出 id、decision（keep/reject/uncertain）和 reason。\n"
    )
    old = filtering.descriptor(old_prompt, job.topic.filter, "model")
    old["harness_version"] = 2
    old_hash = filtering.digest(
        {
            key: old[key]
            for key in ("prompt", "model", "base_url", "max_tokens", "thinking", "temperature")
        }
    )
    new = filtering.descriptor(new_prompt, job.topic.filter, "model")
    assert old_hash in filtering.compatible_hashes(new, [old])
    changed = filtering.descriptor(
        new_prompt + "New classification restriction.", job.topic.filter, "model"
    )
    assert old_hash not in filtering.compatible_hashes(changed, [old])
    assert "每项只引用一个字段" not in filtering.system_prompt(old_prompt)


def test_invalid_provider_json_marks_batch_error_then_continues(setup, monkeypatch):
    job, calls, add = setup
    add(2)
    job.topic.filter.batch_size = 1

    def fail_once(system, items, settings, model):
        calls.append(items)
        if len(calls) == 1:
            raise CollectionError("invalid_model_json", "filter")
        return answer(items)

    monkeypatch.setattr(doubao, "complete_text", fail_once)
    with job.exclusive():
        summary = filtering.run(job)
    assert summary["outcomes"] == {"unclassified": 1, "keep": 1}
    assert summary["stopped"] is None and len(calls) == 2


def test_normalization_is_idempotent_and_preserves_raw_audit(setup):
    job, calls, add = setup
    rows = add(2)
    with job.exclusive():
        filtering.run(job)
    values = job.state.text_filters()
    values[rows[0]["key"]]["result"]["evidence"] = [{"field": "title", "quote": "video 0"}]
    values[rows[1]["key"]].update(status="failed", decision=None, result=None, error="old_error")
    batch = {
        "id": "legacy-audit",
        "status": "failed",
        "response": {
            "results": [
                {
                    "record_id": rows[0]["record_id"],
                    "evidence": [{"field": "title", "quote": "video 0"}],
                }
            ]
        },
    }
    job.state.save_text_filters(values, batch)
    with job.exclusive():
        normalized = job.state.text_filters()
        job.state.recover()
        assert job.state.text_filters() == normalized
        export.write_stage(job.state, job.root, "export", {})
    first = normalized[rows[0]["key"]]
    assert first["result"] == {
        k: values[rows[0]["key"]]["result"][k] for k in ("record_id", "decision", "reason")
    }
    assert first["input_hash"] == values[rows[0]["key"]]["input_hash"]
    assert first["updated_at"] == values[rows[0]["key"]]["updated_at"]
    assert normalized[rows[1]["key"]]["result"] == {
        "record_id": rows[1]["record_id"],
        "decision": "unclassified",
        "reason": "old_error",
    }
    assert (
        next(b for b in jobs.read(job.root)["filter_batches"] if b["id"] == "legacy-audit") == batch
    )
    exported = [json.loads(line) for line in (job.root / "videos.jsonl").read_text().splitlines()]
    assert all(set(r["filter"]["data"]) == {"decision", "reason"} for r in exported)
    assert len(calls) == 1


def test_json_diagnostics_locate_error_without_accepting_partial_json():
    payload = envelope([])
    payload["choices"][0]["finish_reason"] = ""
    payload["choices"][0]["message"]["content"] = (
        '{"results":[{"id":1,"decision":"keep","reason":"unfinished}]}]}'
    )
    diagnostics = []
    results = filtering.validate(payload, ITEMS, diagnostics=diagnostics)
    assert all(r["decision"] == "unclassified" for r in results.values())
    assert diagnostics[0]["code"] == "invalid_json"
    assert diagnostics[0]["finish_reason"] == ""
    assert diagnostics[0]["position"] > 0 and diagnostics[0]["line"] == 1


def test_field_diagnostics_do_not_copy_model_input_values():
    records = json.loads(answer(ITEMS)["choices"][0]["message"]["content"])["results"]
    records[0]["decision"] = "private-invalid-value"
    diagnostics = []
    result = filtering.validate(envelope(records), ITEMS, diagnostics=diagnostics)
    assert result["b"]["decision"] == "unclassified" and result["a"]["decision"] == "keep"
    assert diagnostics[0]["id"] == 2
    assert diagnostics[0]["fields"][0]["field"] == "decision"
    assert "private-invalid-value" not in json.dumps(diagnostics)


def test_transport_diagnostics_survive_export_and_portable_restore(setup, monkeypatch, tmp_path):
    job, calls, add = setup
    add(2)
    detail = {
        "http_status": 429,
        "provider_error": {"code": "TPMExceeded", "message": "rate exceeded"},
        "request_id": "req-123",
        "retry_after": "20",
        "elapsed_seconds": 0.2,
    }

    def limited(*args):
        calls.append(1)
        raise CollectionError("rate_limited", "filter", diagnostics=detail)

    monkeypatch.setattr(doubao, "complete_text", limited)
    with job.exclusive():
        result = filtering.run(job)
    assert result["batch_outcomes"] == {"failed": 1}
    assert result["error_records"] == 2 and result["successful_records"] == 0
    task = jobs.read(job.root)
    assert task["filter_batches"][0]["transport"] == detail
    assert task["filter_errors"] == {
        "records": 2,
        "batches": 1,
        "by_code": {"rate_limited": {"records": 2, "batches": 1}},
    }
    moved = tmp_path / "diagnostics-handoff"
    shutil.copytree(job.root, moved, ignore=shutil.ignore_patterns(".state"))
    restored = Job(job.topic, job.runtime, root=moved)
    with restored.exclusive():
        assert filtering.run(restored)["calls"] == 0
    assert jobs.read(moved)["filter_batches"][0]["transport"] == detail
    assert len(calls) == 1


def test_explicit_text_keep_applies_limit_after_selection(setup, monkeypatch):
    job, calls, add = setup
    assert job.topic.download.selection == "screened"
    job.topic.download.selection = "keep"
    rows = add(5)
    decisions = ["reject", "uncertain", "keep", "keep", "invalid-category"]

    def classify(system, items, settings, model):
        return envelope(
            [
                {
                    "id": item["id"],
                    "decision": decisions[int(item["title"].split()[-1])],
                    "reason": "classification",
                }
                for item in items
            ]
        )

    monkeypatch.setattr(doubao, "complete_text", classify)
    with job.exclusive():
        filtering.run(job)
        stale = json.loads(rows[3]["metadata_json"])
        stale["title"] = "changed after filtering"
        with job.state.connect() as db:
            db.execute(
                "UPDATE resources SET metadata_json=? WHERE key=?",
                (json.dumps(stale), rows[3]["key"]),
            )
        search.import_urls(job, ["https://www.youtube.com/watch?v=abcdef12345"])
        selected = []
        monkeypatch.setattr(
            download, "execute", lambda job, keys, **kwargs: selected.extend(keys) or []
        )
        download.run(job, 1)
        assert selected == [rows[2]["key"]]
        selected.clear()
        download.run(job, 100)
        assert selected == [rows[2]["key"]]
        assert len(job.state.rows()) == 6


def test_trial_duration_limit_applies_before_item_limit(setup, monkeypatch):
    job, calls, add = setup
    rows = add(4)
    with job.state.connect() as db:
        for row, duration in zip(rows, [300, None, 90, 120], strict=True):
            details = json.loads(row["metadata_json"])
            details["duration_seconds"] = duration
            db.execute(
                "UPDATE resources SET metadata_json=? WHERE key=?",
                (json.dumps(details), row["key"]),
            )
    job.topic.download.selection = "keep"
    job.topic.download.max_duration_seconds = 120
    with job.exclusive():
        filtering.run(job)
        selected = []
        monkeypatch.setattr(
            download, "execute", lambda job, keys, **kw: selected.extend(keys) or []
        )
        download.run(job, 1)
        assert selected == [rows[2]["key"]]
        selected.clear()
        download.run(job, 40)
        assert selected == [rows[2]["key"], rows[3]["key"]]
