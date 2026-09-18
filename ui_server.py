#!/usr/bin/env python3
"""Small loopback-only batch UI. No web framework, hosted service, or extra dependency."""

from __future__ import annotations

import argparse
import errno
import json
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from music_pipeline import DEFAULT_ROOT, audio_files, library_lock, resolve_track, track_review, save_review
from youtube_import import COOKIE_BROWSERS, import_video, parse_media_url

WEB = Path(__file__).parent / "web"
ACTIVE = {"queued", "downloading", "tagging"}


def library_path(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("저장 폴더의 전체 경로를 입력해 주세요.")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("전체 경로를 입력해 주세요. 예: ~/Music/minsmix")
    return path.resolve()


class Batch:
    def __init__(self, root=DEFAULT_ROOT):
        self.root = root.expanduser().resolve()
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.jobs = {}
        self.pending = queue.Queue()
        # ponytail: one worker and in-memory history; add persistence only if restart recovery is needed.
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def snapshot(self):
        with self.lock:
            return {"root": str(self.root), "picker": sys.platform == "darwin",
                    "jobs": [dict(job) for job in self.jobs.values()]}

    def add(self, links, folder, browser_cookies=None):
        root = library_path(folder)
        if browser_cookies is not None and (not isinstance(browser_cookies, str) or browser_cookies not in COOKIE_BROWSERS):
            raise ValueError("YouTube 로그인에 사용할 브라우저를 다시 선택해 주세요.")
        if not isinstance(links, str) or len(links) > 32000:
            raise ValueError("링크를 텍스트로 입력해 주세요. 한 번에 최대 50개까지 가능해요.")
        lines = [line.strip() for line in links.splitlines() if line.strip()]
        if not 1 <= len(lines) <= 50:
            raise ValueError("한 줄에 링크 하나씩, 1~50개를 입력해 주세요.")
        result = {"accepted": 0, "duplicates": 0, "invalid": []}
        with self.lock:
            if len(self.jobs) + len(lines) > 500:
                raise ValueError("목록이 가득 찼어요. 완료 목록을 비운 뒤 다시 추가해 주세요.")
            for line in lines:
                try:
                    source = parse_media_url(line)
                except ValueError:
                    result["invalid"].append(line)
                    continue
                url = source.url
                if any(job["url"] == url and job["root"] == str(root)
                       and job["status"] not in {"error", "cancelled"} for job in self.jobs.values()):
                    result["duplicates"] += 1
                    continue
                job_id = secrets.token_hex(8)
                self.jobs[job_id] = {"id": job_id, "url": url, "root": str(root), "status": "queued",
                                     "title": source.label, "artist": "", "progress": None,
                                     "source_id": source.source_id, "source_platform": source.platform,
                                     "path": "", "error": "", "browser_cookies": browser_cookies}
                self.pending.put(job_id)
                result["accepted"] += 1
        return result

    def update(self, job_id, changes):
        with self.lock:
            self.jobs[job_id].update(changes)

    def run(self):
        while True:
            job_id = self.pending.get()
            try:
                if job_id is None:
                    return
                with self.lock:
                    job = self.jobs.get(job_id)
                    if not job or job["status"] != "queued":
                        continue
                    job["status"] = "downloading"
                    url, root = job["url"], Path(job["root"])
                try:
                    outcome = {}
                    def progress(data):
                        if data.get("status") in {"saved", "review", "skipped"}:
                            outcome.update(data)
                        else:
                            self.update(job_id, data)
                    path = import_video(url, root, {}, on_progress=progress, browser_cookies=job.get("browser_cookies"))
                    with self.lock:
                        self.jobs[job_id].update(outcome or {"status": "review" if path.parent.name == "_inbox" else "saved", "path": str(path)})
                except (Exception, SystemExit) as error:
                    message = re.sub(r"\x1b\[[0-9;]*m", "", str(error))[:1500]
                    self.update(job_id, {"status": "error", "progress": None, "error": message or "다운로드를 완료하지 못했어요."})
            finally:
                self.pending.task_done()

    def retry(self, job_id, browser_cookies=None):
        if browser_cookies is not None and (not isinstance(browser_cookies, str) or browser_cookies not in COOKIE_BROWSERS):
            raise ValueError("YouTube 로그인에 사용할 브라우저를 다시 선택해 주세요.")
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["status"] not in {"error", "cancelled"}:
                raise ValueError("실패하거나 취소한 항목만 다시 시도할 수 있어요.")
            if any(other["id"] != job_id and other["url"] == job["url"] and other["root"] == job["root"]
                   and other["status"] in ACTIVE for other in self.jobs.values()):
                raise ValueError("같은 링크가 이미 대기 중이에요.")
            job.update(status="queued", progress=None, error="", browser_cookies=browser_cookies)
            self.pending.put(job_id)

    def cancel_waiting(self):
        with self.lock:
            for job in self.jobs.values():
                if job["status"] == "queued":
                    job["status"] = "cancelled"

    def clear_finished(self):
        with self.lock:
            self.jobs = {key: job for key, job in self.jobs.items() if job["status"] in ACTIVE}

    def remember_track(self, review):
        with self.lock:
            matching = [job for job in self.jobs.values() if job["root"] == review["root"]
                        and (job["path"] in {review["path"], review.get("previous_path")}
                             or review["source_url"] and job["url"] == review["source_url"])]
            if not matching:
                if len(self.jobs) >= 500:
                    raise ValueError("목록이 가득 찼어요. 완료 목록을 비운 뒤 다시 불러와 주세요.")
                job = {"id": secrets.token_hex(8), "root": review["root"]}
                self.jobs[job["id"]] = job
                matching = [job]
            for job in matching:
                if job.get("status") not in ACTIVE:
                    job.update(status=review["status"], path=review["path"], title=review["tags"]["title"] or Path(review["path"]).stem,
                               artist=review["tags"]["artist"], url=review["source_url"], progress=100, error="",
                               source_id=review["source_id"], source_platform=review["source_platform"])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        body = json.dumps(data, ensure_ascii=False).encode() if isinstance(data, dict) else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Minsmix-App", "youtube-dj-importer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, private=False):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.respond(403, {"error": "이 UI는 이 컴퓨터에서만 사용할 수 있어요."})
            return False
        if private:
            origin = self.headers.get("Origin")
            token = self.headers.get("X-Minsmix-Token", "")
            if (origin and origin != f"http://{expected}") or not token.isascii() or not secrets.compare_digest(token, self.server.batch.token):
                self.respond(403, {"error": "연결을 새로 열어 주세요. 페이지를 새로고침하면 돼요."})
                return False
        return True

    def do_GET(self):
        if not self.allowed(private=self.path.startswith("/api/")):
            return
        if self.path == "/api/state":
            self.respond(200, self.server.batch.snapshot())
            return
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path not in assets:
            self.respond(404, {"error": "Not found"})
            return
        name, content_type = assets[self.path]
        text = (WEB / name).read_text(encoding="utf-8").replace("__API_TOKEN__", self.server.batch.token)
        self.respond(200, text.encode(), content_type)

    def do_POST(self):
        if not self.allowed(private=True):
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536 or self.headers.get_content_type() != "application/json":
                raise ValueError("올바른 JSON 요청이 필요해요. 요청 크기는 64KB 이하여야 해요.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("요청 형식이 올바르지 않아요.")
            batch = self.server.batch
            if self.path == "/api/add":
                browser_cookies = data.get("browser_cookies")
                self.respond(200, batch.add(data.get("links"), data.get("root"), None if browser_cookies == "" else browser_cookies))
            elif self.path in {"/api/library", "/api/tag-read", "/api/tag-save"}:
                root = library_path(data.get("root"))
                if not root.is_dir():
                    raise ValueError("음악 폴더가 아직 없어요. 저장 폴더 경로를 확인해 주세요.")
                with library_lock(root):
                    if self.path == "/api/library":
                        files = [p for p in audio_files(root) if p.resolve().is_relative_to(root)]
                        if len(files) > 500:
                            raise ValueError("한 번에 불러올 수 있는 곡은 500개예요. 더 작은 폴더를 선택해 주세요.")
                        reviews = [track_review(root, path) for path in files]
                        for review in reviews:
                            batch.remember_track(review)
                        result = {"count": len(reviews)}
                    else:
                        source_id = data.get("source_id", data.get("video_id", ""))
                        path = resolve_track(root, data.get("path"), source_id, data.get("source_platform", ""))
                        if self.path == "/api/tag-read":
                            result = track_review(root, path)
                        else:
                            result = save_review(root, path, data.get("revision"), data.get("tags"))
                        batch.remember_track(result)
                self.respond(200, result)
            elif self.path == "/api/retry":
                job_id = data.get("id")
                if not isinstance(job_id, str):
                    raise ValueError("항목을 선택해 주세요.")
                browser_cookies = data.get("browser_cookies")
                batch.retry(job_id, None if browser_cookies == "" else browser_cookies)
                self.respond(200, {"ok": True})
            elif self.path == "/api/cancel":
                batch.cancel_waiting()
                self.respond(200, {"ok": True})
            elif self.path == "/api/clear":
                batch.clear_finished()
                self.respond(200, {"ok": True})
            elif self.path == "/api/choose-folder":
                if sys.platform != "darwin":
                    raise ValueError("이 컴퓨터에서는 폴더 경로를 직접 입력해 주세요.")
                try:
                    selected = subprocess.run(["osascript", "-e", "activate", "-e", 'POSIX path of (choose folder with prompt "음악을 저장할 폴더를 선택하세요")'],
                                              capture_output=True, text=True, timeout=120)
                except subprocess.TimeoutExpired:
                    raise ValueError("폴더 선택 시간이 지났어요. 다시 선택하거나 경로를 직접 입력해 주세요.")
                if selected.returncode and "-128" not in selected.stderr:
                    raise ValueError("폴더 선택 창을 열지 못했어요. 경로를 직접 입력해 주세요.")
                self.respond(200, {"root": selected.stdout.strip() if not selected.returncode else None})
            elif self.path == "/api/open":
                path = library_path(data.get("root"))
                if not path.is_dir():
                    raise ValueError("아직 폴더가 없어요. 첫 다운로드가 시작되면 자동으로 만들어요.")
                command = "open" if sys.platform == "darwin" else "xdg-open"
                if not shutil.which(command):
                    raise ValueError("폴더 경로를 복사해서 파일 관리자에서 열어 주세요.")
                subprocess.run([command, str(path)], check=True, timeout=15)
                self.respond(200, {"ok": True})
            else:
                self.respond(404, {"error": "Not found"})
        except SystemExit:
            self.respond(409, {"error": "같은 폴더에서 다운로드나 태그 작업이 진행 중이에요. 끝난 뒤 다시 시도해 주세요. 입력한 값은 그대로 유지돼요."})
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            self.respond(400, {"error": str(error)})


def make_server(port=8765, root=DEFAULT_ROOT):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.batch = Batch(root)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server = make_server(args.port, args.root)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            url = f"http://127.0.0.1:{args.port}"
            try:
                with urlopen(url, timeout=2) as response:
                    if response.headers.get("X-Minsmix-App") == "youtube-dj-importer":
                        print(f"minsmix is already running: {url}")
                        if not args.no_browser:
                            webbrowser.open(url)
                        return
            except OSError:
                pass
        parser.exit(1, f"Could not start the UI: {error}\nIf it is already running, open http://127.0.0.1:{args.port}; otherwise choose another --port.\n")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"\nminsmix is ready: {url}\nDownload folder: {server.batch.root}\nKeep this window open while downloading. Ctrl+C stops the server.\n", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n대기 중인 항목을 취소합니다. 현재 다운로드를 마치면 종료됩니다.")
    finally:
        server.server_close()
        server.batch.cancel_waiting()
        server.batch.pending.put(None)
        server.batch.worker.join()


if __name__ == "__main__":
    main()
