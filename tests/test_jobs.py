import json
import shutil

import yaml

from video_curator import search
from video_curator.cli import main
from video_curator.identity import identify
from video_curator.models import Candidate, SearchBatch, VideoMetadata


def config_files(job, parent):
    topic = parent / "topic.yaml"
    runtime = parent / "runtime.yaml"
    topic.write_text(yaml.safe_dump(job.topic.model_dump(mode="json")))
    runtime.write_text(yaml.safe_dump(job.runtime.model_dump(mode="json")))
    return ["--topic", str(topic), "--runtime", str(runtime)]


def test_cli_job_search_resume_metadata_and_download_handoff(
    collection, tmp_path, monkeypatch, capsys
):
    job, details, calls = collection
    common = config_files(job, tmp_path)
    root = tmp_path / "job"
    page_calls = []

    def native(request, runtime):
        page_calls.append(request.page)
        urls = (
            [details.access_url]
            if request.page == 1
            else [details.access_url, "https://www.youtube.com/watch?v=abcdefghijk"]
        )
        return SearchBatch(
            request=request,
            pagination="page",
            next_page=2 if request.page == 1 else None,
            stop_reason="page_limit" if request.page == 1 else "returned_batch_end",
            candidates=[
                Candidate(
                    platform="youtube",
                    url=url,
                    query=request.query,
                    mode="native",
                    position=f"{request.page}:{i}",
                )
                for i, url in enumerate(urls)
            ],
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", native)
    args = common + [
        "search",
        "--platform",
        "youtube",
        "--all-pages",
        "--output",
        str(root),
    ]
    assert main(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["candidate_records"] == 2
    assert summary["queries_planned"] == 1
    assert page_calls == [1, 2]
    assert len((root / "videos.jsonl").read_text().splitlines()) == 2
    records = [json.loads(line) for line in (root / "videos.jsonl").read_text().splitlines()]
    assert sum(len(r["search"]["data"]) for r in records) == 3
    assert {p.name for p in root.iterdir()} == {"videos.jsonl", "task.json", ".state"}
    ids = {r["record_id"] for r in records}
    assert main(args) == 0
    capsys.readouterr()
    assert page_calls == [1, 2]

    # The next task receives only the moved directory and its snapshots.
    moved = tmp_path / "handoff"
    root.rename(moved)
    shutil.rmtree(moved / ".state")
    root = moved
    (tmp_path / "topic.yaml").unlink()
    assert not (root / "judge.md").exists()

    def fetch(candidate, runtime):
        calls["metadata"] += 1
        return VideoMetadata(
            identity=identify(candidate.url),
            access_url=candidate.url,
            title="robot performance",
            duration_seconds=2,
        )

    monkeypatch.setattr("video_curator.metadata.fetch", fetch)
    assert main(["metadata", "--input", str(root)]) == 0
    capsys.readouterr()
    assert calls["metadata"] == 2
    assert len((root / "videos.jsonl").read_text().splitlines()) == 2
    assert main(["metadata", "--input", str(root)]) == 0
    capsys.readouterr()
    assert calls["metadata"] == 2
    assert (
        main(["download", "--input", str(root), "--selection", "non-rejected", "--limit", "2"]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["downloaded_records"] == 2
    assert calls == {"metadata": 2, "download": 2, "screen": 0}
    task = json.loads((root / "task.json").read_text())
    assert task["stages"]["download"]["downloaded_records"] == 2
    records = [json.loads(line) for line in (root / "videos.jsonl").read_text().splitlines()]
    assert {r["record_id"] for r in records} == ids
    assert {p.name for p in root.iterdir()} == {
        "videos.jsonl",
        "task.json",
        ".state",
        "raw",
    }


def test_all_pages_on_limit_entry_is_explicitly_partial(collection, tmp_path, monkeypatch, capsys):
    job, details, _ = collection
    common = config_files(job, tmp_path)
    monkeypatch.setitem(
        search.NATIVE_SEARCH,
        "youtube",
        lambda request, runtime: SearchBatch(
            request=request,
            candidates=[Candidate(platform="youtube", url=details.access_url)],
            pagination="limit",
            stop_reason="limit_reached",
        ),
    )
    root = tmp_path / "job"
    assert (
        main(common + ["search", "--platform", "youtube", "--all-pages", "--output", str(root)])
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["stopped"] == "all_pages_not_supported_for_entry"
    assert (root / "videos.jsonl").is_file()


def test_job_metadata_processes_missing_rows_even_after_ready_limit(
    collection, tmp_path, monkeypatch, capsys
):
    job, details, calls = collection
    common = config_files(job, tmp_path)
    root = tmp_path / "job"
    monkeypatch.setitem(
        search.NATIVE_SEARCH,
        "youtube",
        lambda request, runtime: SearchBatch(
            request=request,
            candidates=[
                Candidate(platform="youtube", url=url)
                for url in [details.access_url, "https://www.youtube.com/watch?v=abcdefghijk"]
            ],
            pagination="limit",
            stop_reason="limit_reached",
        ),
    )
    assert main(common + ["search", "--platform", "youtube", "--output", str(root)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(
        "video_curator.metadata.fetch",
        lambda candidate, runtime: VideoMetadata(
            identity=identify(candidate.url), access_url=candidate.url, title="robot"
        ),
    )
    for _ in range(2):
        assert main(["metadata", "--input", str(root), "--limit", "1"]) == 0
        capsys.readouterr()
    summary = json.loads((root / "task.json").read_text())["stages"]["metadata"]
    assert summary["metadata_records"] == 2


def test_partial_job_resumes_at_failed_page_and_preserves_error_history(
    collection, tmp_path, monkeypatch, capsys
):
    from video_curator.models import CollectionError

    job, details, _ = collection
    common = config_files(job, tmp_path)
    root = tmp_path / "job"
    calls = []

    def native(request, runtime):
        calls.append(request.page)
        if calls == [1, 2]:
            raise CollectionError("platform_access_blocked", "search")
        url = (
            details.access_url
            if request.page == 1
            else "https://www.youtube.com/watch?v=abcdefghijk"
        )
        return SearchBatch(
            request=request,
            candidates=[Candidate(platform="youtube", url=url)],
            pagination="page",
            next_page=2 if request.page == 1 else None,
            stop_reason="page_limit" if request.page == 1 else "returned_batch_end",
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", native)
    args = common + [
        "search",
        "--platform",
        "youtube",
        "--all-pages",
        "--output",
        str(root),
    ]
    assert main(args) == 2
    capsys.readouterr()
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert calls == [1, 2, 2]
    assert result["errors"] == {} and result["historical_error_records"] == 1


def test_cli_progress_precedes_request_and_keeps_stdout_json(
    collection, tmp_path, monkeypatch, capsys
):
    _, details, _ = collection
    calls = []

    def native(request, runtime):
        before = capsys.readouterr()
        assert before.out == ""
        assert "启动" in before.err and "正在请求" in before.err
        calls.append(request.query)
        return SearchBatch(
            request=request,
            candidates=[Candidate(platform="youtube", url=details.access_url)] * 2,
            pagination="limit",
            stop_reason="limit_reached",
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", native)
    args = [
        "search",
        "--topic",
        "robot",
        "--platform",
        "youtube",
        "--output",
        str(tmp_path / "job"),
    ]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["candidate_records"] == 1
    assert "返回 2，新增 1，重复 1" in captured.err
    assert "已保存 videos.jsonl 和 task.json" in captured.err
    assert main(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["candidate_records"] == 1
    assert "复用已完成结果" in captured.err
    assert calls == ["robot"]
    assert main(["metadata", "--input", str(tmp_path / "job")]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["metadata_records"] == 1
    assert "正在获取" in captured.err and "成功" in captured.err


def test_task_cache_isolated_and_invalidated_by_atomic_replacement(tmp_path, monkeypatch):
    from pathlib import Path

    from video_curator import export, jobs
    from video_curator.config import Runtime, Topic

    root = tmp_path / "task"
    jobs.initialize(root, Topic(id="test"), Runtime())
    jobs._read_cached.cache_clear()
    original_read = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path.name == "task.json":
            reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    first = jobs.read(root)
    first["topic"]["id"] = "local-mutation"
    assert jobs.read(root)["topic"]["id"] == "test"
    jobs.load(root)
    assert len(reads) == 1
    saved = jobs.read(root)
    saved["topic"]["download"]["workers"] = 3
    export.write_json(root / "task.json", saved)
    assert jobs.load(root)[0].download.workers == 3
    assert len(reads) == 2
