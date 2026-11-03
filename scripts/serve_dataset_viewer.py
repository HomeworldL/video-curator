#!/usr/bin/env python3
"""Serve a live read-only dataset viewer. No exports, model calls, or dataset writes."""

import argparse
import errno
import hashlib
import json
import mimetypes
import sqlite3
import threading
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from build_dataset_viewer import project

from video_collect import export, screening


class ReadOnlyState:
    """Use one read transaction for both records and configuration."""

    def __init__(self, db):
        self.db = db

    @contextmanager
    def connect(self):
        yield self.db

    def setting(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def signature(path):
    try:
        st = path.stat()
        return (st.st_ino, st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return None


class DatasetSource:
    def __init__(self, root):
        self.root = root.resolve()
        self.database = self.root / ".state/state.sqlite"
        if not self.database.is_file() and not (self.root / "videos.jsonl").is_file():
            raise ValueError("input must contain state.sqlite or videos.jsonl")
        self.lock = threading.Lock()
        self.version = self.body = None
        self.task_version = None
        self.stages = {}
        self.db = None
        self.db_identity = None

    def connection(self):
        identity = signature(self.database)
        inode = identity[0] if identity else None
        if inode != self.db_identity:
            if self.db is not None:
                self.db.close()
            self.db = None
            self.db_identity = inode
        if inode is not None and self.db is None:
            self.db = sqlite3.connect(
                self.database.as_uri() + "?mode=ro",
                uri=True,
                timeout=5,
                check_same_thread=False,
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA query_only=ON")
        return self.db

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None

    def fingerprint(self):
        db = self.connection()
        data_version = db.execute("PRAGMA data_version").fetchone()[0] if db else None
        paths = [self.database, self.root / "task.json"]
        if not self.database.is_file():
            paths.append(self.root / "videos.jsonl")
        return (
            '"'
            + hashlib.sha256(
                repr((data_version, [signature(p) for p in paths])).encode()
            ).hexdigest()
            + '"'
        )

    def payload(self):
        task_path = self.root / "task.json"
        version = signature(task_path)
        if version != self.task_version:
            self.stages = json.loads(task_path.read_text()).get("stages", {}) if version else {}
            self.task_version = version
        usage = {
            stage: {
                k: v
                for k, v in (self.stages.get(stage) or {}).items()
                if k
                in {
                    "usage",
                    "cumulative",
                    "calls",
                    "usage_reported_calls",
                    "usage_missing_calls",
                    "updated_at",
                }
            }
            for stage in ("filter", "screen")
        }
        in_flight = 0
        if self.database.is_file():
            db = self.connection()
            try:
                db.execute("BEGIN")
                state = ReadOnlyState(db)
                records, _ = export.snapshot(state, self.root, include_resume=False)
                values = [
                    json.loads(r[0]) for r in db.execute("SELECT data_json FROM screen_results")
                ]
                usage["screen"]["cumulative"] = screening.usage_totals(values)
                in_flight = sum(v["status"] == "in_flight" for v in values)
                progress = state.setting("filter_progress")
                if progress:
                    latest = json.loads(progress)
                    usage["filter"] = {
                        k: v for k, v in latest.items() if k in {"usage", "calls", "updated_at"}
                    }
            finally:
                db.rollback()  # End only this read snapshot; keep data_version comparable.
            origin = "SQLite"
        else:
            with (self.root / "videos.jsonl").open() as stream:
                records = [json.loads(line) for line in stream if line.strip()]
            if any(r.get("schema") != "video_collect.video.v3" for r in records):
                raise ValueError("viewer requires video_collect.video.v3")
            origin = "videos.jsonl"
        return {
            "name": self.root.name,
            "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "live": True,
            "source": origin,
            "in_flight": in_flight,
            "usage": usage,
            "records": [
                project(r, self.root, self.root / "index.html")
                for r in sorted(records, key=lambda r: r["record_id"])
            ],
        }

    def response(self, etag):
        with self.lock:
            version = self.fingerprint()
            if self.body is None or self.version != version:
                body = json.dumps(
                    self.payload(), ensure_ascii=False, separators=(",", ":")
                ).encode()
                self.body, self.version = body, version
            return self.version, None if etag == self.version else self.body


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, source):
        self.source = source
        template = Path(__file__).with_name("dataset_viewer.html").read_text()
        self.html = template.replace(
            "__DATASET_JSON__",
            json.dumps(
                {
                    "name": source.root.name,
                    "records": [],
                    "usage": {},
                    "generated": "",
                    "live": True,
                },
                ensure_ascii=True,
            ).replace("<", "\\u003c"),
        ).encode()
        super().__init__(address, ViewerHandler)

    def server_close(self):
        super().server_close()
        self.source.close()


class ViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Avoid a terminal log line for every image and refresh.

    def send_bytes(self, status, content, mime, **headers):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = unquote(urlsplit(self.path).path)
        try:
            if route in {"/", "/index.html"}:
                self.send_bytes(200, self.server.html, "text/html; charset=utf-8")
            elif route == "/api/dataset":
                etag, body = self.server.source.response(self.headers.get("If-None-Match"))
                if body is None:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                else:
                    self.send_bytes(200, body, "application/json; charset=utf-8", ETag=etag)
            else:
                self.serve_media(route)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            # A transient snapshot error must not replace the browser's previous data.
            self.send_bytes(
                503,
                json.dumps(
                    {"error": type(exc).__name__, "message": "读取数据暂时失败，稍后自动重试"},
                    ensure_ascii=False,
                ).encode(),
                "application/json; charset=utf-8",
            )

    def serve_media(self, route):
        # Serve only known media directories, never task.json, SQLite, or credentials.
        relative = Path(route.lstrip("/"))
        if not relative.parts or relative.parts[0] not in {"evidence", "raw"}:
            self.send_error(404)
            return
        root = self.server.source.root
        path = (root / relative).resolve()
        if (
            not path.is_relative_to(root / relative.parts[0])
            or not path.is_file()
            or path.suffix.lower()
            not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mkv", ".mov"}
        ):
            self.send_error(404)
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get("Range")
        if requested:
            try:
                unit, value = requested.split("=", 1)
                first, last = value.split("-", 1)
                if unit != "bytes" or "," in value:
                    raise ValueError
                if first:
                    start = int(first)
                    end = min(int(last), end) if last else end
                else:
                    length = int(last)
                    if length <= 0:
                        raise ValueError
                    start = max(0, size - length)
                if not 0 <= start <= end < size:
                    raise ValueError
            except ValueError:
                self.send_bytes(416, b"", "text/plain", Content_Range=f"bytes */{size}")
                return
        self.send_response(206 if requested else 200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command != "HEAD":
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        default=8765,
        type=int,
        help="0 selects any free port; occupied ports fall back automatically",
    )
    parser.add_argument("--open", action="store_true", help="Open the local browser")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    source = DatasetSource(args.input)
    try:
        server = ViewerServer((args.host, args.port), source)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        server = ViewerServer((args.host, 0), source)
    host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = f"http://{host}:{server.server_port}/"
    print(f"数据集网页：{url}\n只读；每10秒检查更新；Ctrl+C退出。", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
