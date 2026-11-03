import json

import pytest
import yaml

from video_curator import jobs, search
from video_curator.cli import main
from video_curator.config import Runtime, resolve_topic
from video_curator.models import Candidate, CollectionError, SearchBatch


@pytest.mark.parametrize("after_command", [False, True])
def test_yaml_topic_search_runs_all_terms_without_all_flag(
    collection, tmp_path, monkeypatch, capsys, after_command
):
    job, _, _ = collection
    job.topic.search.shared_queries = ["robot animation", "机器人 舞蹈"]
    job.topic.search.queries = {}
    topic_path = tmp_path / "topic.yaml"
    topic_path.write_text(yaml.safe_dump(job.topic.model_dump(mode="json")))
    requests = []

    def discover(request, runtime):
        requests.append(request)
        return SearchBatch(
            request=request, candidates=[], pagination="limit", stop_reason="returned_batch_end"
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", discover)
    prefix = (
        ["search", "--topic", str(topic_path)]
        if after_command
        else ["--topic", str(topic_path), "search"]
    )
    assert main(prefix + ["--platform", "youtube", "--output", str(tmp_path / "job")]) == 0
    capsys.readouterr()
    assert [request.query for request in requests] == job.topic.search.shared_queries


@pytest.mark.parametrize("text", ["LABUBU animation", "机器狗 运动"])
def test_literal_topic_is_a_single_query_and_survives_folder_handoff(
    collection, tmp_path, monkeypatch, capsys, text
):
    _, details, calls = collection
    requests = []

    def discover(request, runtime):
        requests.append(request)
        return SearchBatch(
            request=request,
            candidates=[Candidate(platform="youtube", url=details.access_url)],
            pagination="limit",
            stop_reason="limit_reached",
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", discover)
    root = tmp_path / "job"
    assert main(["search", "--topic", text, "--platform", "youtube", "--output", str(root)]) == 0
    capsys.readouterr()
    assert [request.query for request in requests] == [text]
    saved, _ = jobs.load(root)
    assert saved.search.shared_queries == [text]
    assert saved.screen.prompt is None and saved.id == resolve_topic(text).id
    assert main(["metadata", "--input", str(root)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["metadata_records"] == 1 and calls["screen"] == 0


def test_missing_yaml_is_an_error_instead_of_a_literal_query(tmp_path, capsys):
    assert main(["search", "--topic", str(tmp_path / "missing.yaml"), "--platform", "youtube"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "topic_file_not_found"


def test_other_topic_file_does_not_add_default_labubu_terms(collection, tmp_path):
    job, _, _ = collection
    path = tmp_path / "robots.yml"
    path.write_text(yaml.safe_dump(job.topic.model_dump(mode="json")))
    topic = resolve_topic(path)
    assert topic.search.queries_for("youtube") == ["robot performance"]
    assert "LABUBU" not in topic.search.shared_queries


def test_default_topic_and_portable_snapshot_include_inline_prompt(tmp_path):
    topic = resolve_topic(None)
    assert topic.id == "labubu"
    assert len(topic.search.shared_queries) == 27
    assert "\n" in topic.screen.prompt and "difficult" in topic.screen.prompt
    root = tmp_path / "job"
    jobs.initialize(root, topic, Runtime())
    moved = tmp_path / "moved"
    root.rename(moved)
    saved, _ = jobs.load(moved)
    assert saved.screen.prompt == topic.screen.prompt
    assert not (moved / "judge.md").exists()
    snapshot = json.loads((moved / "task.json").read_text())["topic"]
    assert snapshot["screen"]["prompt"] == topic.screen.prompt


def test_changed_search_scope_cannot_silently_reuse_task(collection, tmp_path):
    job, _, _ = collection
    root = tmp_path / "job"
    jobs.initialize(root, job.topic, job.runtime)
    job.topic.screen.prompt = "新视觉标准不影响搜索恢复。"
    jobs.initialize(root, job.topic, job.runtime)
    job.topic.search.shared_queries.append("new search scope")
    with pytest.raises(CollectionError, match="job_topic_changed_use_new_output"):
        jobs.initialize(root, job.topic, job.runtime)


def test_minimal_yaml_needs_no_rules_version_or_prompt(tmp_path, monkeypatch, capsys):
    path = tmp_path / "robots.yaml"
    path.write_text("id: robots\nsearch:\n  shared_queries: [机器狗 运动]\n")
    requests = []

    def discover(request, runtime):
        requests.append(request)
        return SearchBatch(
            request=request, candidates=[], pagination="limit", stop_reason="returned_batch_end"
        )

    monkeypatch.setitem(search.NATIVE_SEARCH, "youtube", discover)
    root = tmp_path / "job"
    assert (
        main(["search", "--topic", str(path), "--platform", "youtube", "--output", str(root)]) == 0
    )
    assert json.loads(capsys.readouterr().out)["queries_planned"] == 1
    assert [r.query for r in requests] == ["机器狗 运动"]
    topic, _ = jobs.load(root)
    assert topic.screen.prompt is None
    assert "rules" not in topic.model_dump() and "version" not in topic.model_dump()
