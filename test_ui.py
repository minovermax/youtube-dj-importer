"""Offline check of the HTTP boundary and mixed-success batch queue."""

import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import ui_server
import music_pipeline
from mutagen.id3 import GEOB, ID3
from test_import import audio_hash
from youtube_import import embed_tags, parse_media_url


def check():
    with tempfile.TemporaryDirectory(prefix="minsmix-ui-test-") as directory:
        root = Path(directory).resolve()
        entered, release = threading.Event(), threading.Event()
        calls = []
        attempts = {}
        tone = root / ".test-tone.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1", str(tone)], check=True)

        cookie_calls = []

        def fake_import(url, folder, overrides, *, on_progress, browser_cookies=None):
            source = parse_media_url(url)
            source_id = source.source_id or "62986583"
            calls.append(source_id)
            cookie_calls.append(browser_cookies)
            attempts[source_id] = attempts.get(source_id, 0) + 1
            if source_id == "aaaaaaaaaaa":
                entered.set()
                assert release.wait(10), "Test did not release the worker"
            if source_id == "bbbbbbbbbbb" and attempts[source_id] == 1:
                raise RuntimeError("Simulated unavailable video")
            on_progress({"status": "downloading", "progress": 50, "title": '<script>alert("no")</script>'})
            on_progress({"status": "tagging", "progress": None})
            folder = folder / ("_inbox" if source_id == "ccccccccccc" else "tracks")
            folder.mkdir(exist_ok=True)
            path = folder / f"test [{source_id}].mp3"
            shutil.copy2(tone, path)
            embed_tags(path, {"artist": "" if source_id == "ccccccccccc" else "Artist", "title": "Original (Remix)",
                             "album": "Unverified album", "genre": "", "comment": f"Source: {url} | {'Track' if source.platform == 'soundcloud' else 'Video'} title: Original (Remix) | Metadata: needs review | Review: missing artist"},
                       url, source_id, platform=source.platform)
            on_progress({"status": "review" if source_id == "ccccccccccc" else "saved", "path": str(path),
                         "source_id": source_id, "source_platform": source.platform, "url": url})
            return path

        with patch.object(ui_server, "import_video", side_effect=fake_import):
            server = ui_server.make_server(0, root)
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def request(path, data=None, *, authenticated=True, headers=None):
                supplied = {"Content-Type": "application/json"}
                if authenticated:
                    supplied["X-Minsmix-Token"] = server.batch.token
                supplied.update(headers or {})
                req = Request(base + path, headers=supplied, data=json.dumps(data).encode() if data is not None else None)
                try:
                    with urlopen(req, timeout=5) as result:
                        return result.status, result.read(), result.headers
                except HTTPError as error:
                    return error.code, error.read(), error.headers

            try:
                code, page, headers = request("/", authenticated=False)
                assert code == 200 and server.batch.token.encode() in page
                assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
                assert request("/api/state", authenticated=False)[0] == 403
                assert request("/api/add", {}, headers={"Origin": "https://evil.example"})[0] == 403
                assert request("/", headers={"Host": "evil.example"})[0] == 403
                assert request("/api/state", headers={"X-Minsmix-Token": "é"})[0] == 403
                assert request("/../music_pipeline.py")[0] == 404
                assert request("/api/add", {"links": "https://youtu.be/aaaaaaaaaaa", "root": "relative/path"})[0] == 400
                assert request("/api/add", {"links": "https://youtu.be/aaaaaaaaaaa", "root": str(root), "browser_cookies": "unknown"})[0] == 400
                assert request("/api/add", {"links": "https://youtu.be/aaaaaaaaaaa", "root": str(root), "browser_cookies": []})[0] == 400
                assert request("/api/add", {"links": "\n".join(["https://youtu.be/aaaaaaaaaaa"] * 51), "root": str(root)})[0] == 400
                with patch.object(ui_server.sys, "platform", "darwin"):
                    with patch.object(ui_server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=str(root) + "\n", stderr="")):
                        assert json.loads(request("/api/choose-folder", {})[1])["root"] == str(root)
                    with patch.object(ui_server.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="User canceled (-128)")):
                        assert json.loads(request("/api/choose-folder", {})[1])["root"] is None
                    with patch.object(ui_server.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 120)):
                        code, body, _ = request("/api/choose-folder", {})
                        assert code == 400 and "시간이 지났어요" in json.loads(body)["error"]

                code, body, _ = request("/api/add", {"links": "https://youtu.be/aaaaaaaaaaa\nhttps://youtube.com/watch?v=aaaaaaaaaaa&list=ignore\nhttps://evil.example/\nhttps://youtu.be/bbbbbbbbbbb\nhttps://youtu.be/ccccccccccc", "root": str(root), "browser_cookies": "chrome"})
                result = json.loads(body)
                assert code == 200 and result == {"accepted": 3, "duplicates": 1, "invalid": ["https://evil.example/"]}
                assert entered.wait(5)
                state = json.loads(request("/api/state")[1])
                assert [job["status"] for job in state["jobs"]] == ["downloading", "queued", "queued"]
                assert request("/api/clear", {})[0] == 200
                assert len(server.batch.snapshot()["jobs"]) == 3
                release.set()

                def wait_idle():
                    end = time.monotonic() + 10
                    while time.monotonic() < end:
                        if not any(job["status"] in ui_server.ACTIVE for job in server.batch.snapshot()["jobs"]):
                            return
                        time.sleep(.02)
                    raise AssertionError("Queue did not finish")

                wait_idle()
                state = server.batch.snapshot()
                assert [job["status"] for job in state["jobs"]] == ["saved", "error", "review"]
                assert calls == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
                assert cookie_calls == ["chrome", "chrome", "chrome"]
                failed_id = state["jobs"][1]["id"]
                assert request("/api/retry", {"id": failed_id, "browser_cookies": "firefox"})[0] == 200
                wait_idle()
                assert server.batch.snapshot()["jobs"][1]["status"] == "saved"
                assert cookie_calls[-1] == "firefox"
                assert request("/api/retry", {"id": []})[0] == 400
                review_path = root / "_inbox" / "test [ccccccccccc].mp3"
                first_path = root / "tracks" / "test [aaaaaaaaaaa].mp3"
                music_pipeline.write_manifest(root)
                draft = music_pipeline.load_manifest(root / "metadata.csv")[first_path.name]
                draft["genre"] = "Other pending CSV edit"
                music_pipeline.write_manifest(root, {first_path.name: draft})
                custom = ID3(review_path)
                custom.add(GEOB(encoding=1, mime="application/octet-stream", filename="", desc="Serato Markers2", data=b"keep-my-cues"))
                custom.save(review_path, v2_version=3)
                original_audio = audio_hash(review_path)
                other_file = first_path.read_bytes()
                selection = {"root": str(root), "path": str(review_path), "video_id": "ccccccccccc"}
                assert request("/api/tag-read", selection, authenticated=False)[0] == 403
                assert request("/api/tag-read", {**selection, "path": "../../outside.mp3"})[0] == 400
                assert request("/api/library", {"root": str(root)})[0] == 200
                review = json.loads(request("/api/tag-read", selection)[1])
                assert review["tags"]["artist"] == "" and review["source_url"].endswith("ccccccccccc")
                values = {"artist": "Producer", "title": "Original (DJ Remix)", "album": "", "genre": "Drum & Bass"}
                assert request("/api/tag-save", {**selection, "revision": review["revision"], "tags": {**values, "artist": " "}})[0] == 400
                code, body, _ = request("/api/tag-save", {**selection, "revision": review["revision"], "tags": values})
                assert code == 200, body
                saved = json.loads(body)
                saved_path = Path(saved["path"])
                assert saved_path.parent == root / "tracks" and not review_path.exists()
                assert saved["status"] == "saved" and saved["tags"] == values
                assert Path(saved["backup"]).is_dir()
                assert audio_hash(saved_path) == original_audio
                assert first_path.read_bytes() == other_file
                assert ID3(saved_path)["GEOB:Serato Markers2"].data == b"keep-my-cues"
                assert str(ID3(saved_path)["TXXX:YouTube ID"]) == "ccccccccccc"
                assert "TALB" not in ID3(saved_path), "An explicitly cleared album must be removed"
                assert music_pipeline.load_manifest(root / "metadata.csv")[first_path.name]["genre"] == "Other pending CSV edit"
                assert request("/api/tag-save", {**selection, "path": str(saved_path), "revision": review["revision"], "tags": values})[0] == 400
                assert any(job["status"] == "saved" and job["path"] == str(saved_path) for job in server.batch.snapshot()["jobs"])
                renamed = saved_path.with_name("renamed.mp3")
                saved_path.rename(renamed)
                reloaded = json.loads(request("/api/tag-read", {**selection, "path": str(saved_path)})[1])
                assert reloaded["path"] == str(renamed) and reloaded["tags"]["artist"] == "Producer"

                rollback = root / "_inbox" / "rollback.mp3"
                shutil.copy2(tone, rollback)
                music_pipeline.write_manifest(root)
                rollback_bytes, csv_bytes = rollback.read_bytes(), (root / "metadata.csv").read_bytes()
                rollback_view = json.loads(request("/api/tag-read", {"root": str(root), "path": str(rollback)})[1])
                with patch.object(music_pipeline, "write_manifest", side_effect=RuntimeError("Simulated CSV failure")):
                    code, body, _ = request("/api/tag-save", {"root": str(root), "path": str(rollback), "revision": rollback_view["revision"], "tags": values})
                assert code == 400 and "복원" in json.loads(body)["error"]
                assert rollback.read_bytes() == rollback_bytes and (root / "metadata.csv").read_bytes() == csv_bytes
                assert not (root / "tracks" / rollback.name).exists()
                assert request("/api/clear", {})[0] == 200
                assert server.batch.snapshot()["jobs"] == []
                assert len(music_pipeline.audio_files(root)) == 4, "Clearing history must not delete audio or scan backups"

                code, body, _ = request("/api/add", {
                    "links": ("https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy?si=tracking\n"
                              "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy?utm_source=clipboard"),
                    "root": str(root),
                })
                assert code == 200 and json.loads(body) == {"accepted": 1, "duplicates": 1, "invalid": []}
                wait_idle()
                soundcloud_job = server.batch.snapshot()["jobs"][-1]
                assert soundcloud_job["status"] == "saved"
                assert soundcloud_job["source_platform"] == "soundcloud" and soundcloud_job["source_id"] == "62986583"
                soundcloud_review = json.loads(request("/api/tag-read", {"root": str(root), "path": soundcloud_job["path"],
                                                                          "source_id": "62986583", "source_platform": "soundcloud"})[1])
                assert soundcloud_review["source_platform"] == "soundcloud"
                assert soundcloud_review["source_url"] == "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy"
                assert request("/api/clear", {})[0] == 200

                entered.clear()
                release.clear()
                request("/api/add", {"links": "https://youtu.be/aaaaaaaaaaa\nhttps://youtu.be/ddddddddddd", "root": str(root)})
                assert entered.wait(5)
                assert request("/api/cancel", {})[0] == 200
                assert [job["status"] for job in server.batch.snapshot()["jobs"]] == ["downloading", "cancelled"]
                release.set()
                wait_idle()
                assert "ddddddddddd" not in calls
                assert request("/api/choose-folder", {}, authenticated=False)[0] == 403
            finally:
                release.set()
                server.batch.cancel_waiting()
                server.batch.pending.put(None)
                server.batch.worker.join(timeout=10)
                server.shutdown()
                server.server_close()
                serving.join(timeout=5)
    print("PASS: HTTP auth, YouTube/SoundCloud queueing, path boundaries, retry/cancel, tag editing, source/cue preservation, unchanged audio, CSV sync, backup/rollback, stale-edit protection, renamed-file recovery.")


if __name__ == "__main__":
    check()
