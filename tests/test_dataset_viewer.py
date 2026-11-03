import json
import threading

import httpx
import pytest

from video_curator import export, search


@pytest.fixture(scope="session")
def viewer(load_script):
    return load_script("dataset_viewer")


def test_report_handles_not_run_and_untrusted_titles_without_mutating_dataset(
    collection, tmp_path, viewer
):
    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    details = details.model_copy(update={"title": '</script><script>alert("x")</script>'})
    job.state.merge_metadata(details.identity.key, details)
    export.write_stage(job.state, job.root, "metadata", {"status": "complete"})
    source = job.root / "videos.jsonl"
    before = source.read_bytes()
    output = tmp_path / "report/index.html"
    result = viewer.build(job.root, output)
    assert result["records"] == 1 and result["screen"] == {"not_run": 1}
    html = output.read_text()
    assert details.title not in html
    payload = html.split('<script type="application/json" id="dataset">')[1].split("</script>")[0]
    record = json.loads(payload)["records"][0]
    assert record["title"] == details.title
    assert record["preview"] is None
    assert source.read_bytes() == before
    assert viewer.web_url("javascript:alert(1)") is None
    assert viewer.local_url("../report/index.html", job.root, output) is None
    image = job.root / "evidence" / "a b.jpg"
    image.parent.mkdir()
    image.write_bytes(b"fixture")
    assert viewer.local_url(str(image), job.root, output).endswith("a%20b.jpg")


@pytest.fixture
def viewer_server(collection, viewer):
    job, details, _ = collection
    search.import_urls(job, [details.access_url])
    job.state.merge_metadata(details.identity.key, details)
    export.write_stage(job.state, job.root, "metadata", {"status": "complete"})
    server = viewer.ViewerServer(("127.0.0.1", 0), viewer.DatasetSource(job.root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False) as client:
        yield client, job, details
    server.shutdown()
    server.server_close()
    thread.join()


def test_live_database_updates_without_export_and_reuses_etag(viewer_server):
    client, job, details = viewer_server
    before = (job.root / "videos.jsonl").read_bytes()
    response = client.get("/api/dataset")
    assert response.status_code == 200
    assert response.json()["records"][0]["title"] == details.title
    assert response.json()["source"] == "SQLite"
    etag = response.headers["etag"]
    assert client.get("/api/dataset", headers={"If-None-Match": etag}).status_code == 304
    # A writer can hold its normal run lock while the viewer reads committed data.
    with job.exclusive():
        with job.state.connect() as db:
            db.execute(
                "UPDATE resources SET metadata_json=? WHERE key=?",
                (
                    details.model_copy(update={"title": "Updated live"}).model_dump_json(),
                    details.identity.key,
                ),
            )
        changed = client.get("/api/dataset", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.json()["records"][0]["title"] == "Updated live"
    assert changed.headers["etag"] != etag
    assert before == (job.root / "videos.jsonl").read_bytes()
    assert client.get("/").status_code == 200
    assert client.post("/api/dataset", json={}).status_code == 501


def test_only_media_is_served_with_byte_ranges(viewer_server):
    client, job, _ = viewer_server
    media = job.root / "evidence" / "test.jpg"
    media.parent.mkdir()
    media.write_bytes(b"0123456789")
    response = client.get("/evidence/test.jpg", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert client.head("/evidence/test.jpg").headers["content-length"] == "10"
    assert client.get("/evidence/test.jpg", headers={"Range": "bytes=15-"}).status_code == 416
    for path in ["/task.json", "/.state/state.sqlite", "/evidence/%2e%2e/task.json"]:
        assert client.get(path).status_code == 404
    link = media.parent / "outside.jpg"
    link.symlink_to(job.root / "task.json")
    assert client.get("/evidence/outside.jpg").status_code == 404


def test_jsonl_fallback(viewer, tmp_path):
    root = tmp_path / "portable"
    root.mkdir()
    (root / "videos.jsonl").write_text("")
    source = viewer.DatasetSource(root)
    etag, body = source.response(None)
    assert json.loads(body)["source"] == "videos.jsonl"
    assert source.response(etag)[1] is None
