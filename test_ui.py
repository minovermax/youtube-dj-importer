"""Offline check of the HTTP boundary and mixed-success batch queue."""

import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import ui_server


def check():
    with tempfile.TemporaryDirectory(prefix="minsmix-ui-test-") as directory:
        root = Path(directory).resolve()
        entered, release = threading.Event(), threading.Event()
        calls = []
        attempts = {}

        def fake_import(url, folder, overrides, *, on_progress):
            video_id = url.split("v=")[1]
            calls.append(video_id)
            attempts[video_id] = attempts.get(video_id, 0) + 1
            if video_id == "aaaaaaaaaaa":
                entered.set()
                assert release.wait(10), "Test did not release the worker"
            if video_id == "bbbbbbbbbbb" and attempts[video_id] == 1:
                raise RuntimeError("Simulated unavailable video")
            on_progress({"status": "downloading", "progress": 50, "title": '<script>alert("no")</script>'})
            on_progress({"status": "tagging", "progress": None})
            folder = folder / ("_inbox" if video_id == "ccccccccccc" else "tracks")
            folder.mkdir(exist_ok=True)
            path = folder / f"test [{video_id}].mp3"
            path.write_bytes(b"Test-only sentinel, not audio")
            on_progress({"status": "review" if video_id == "ccccccccccc" else "saved", "path": str(path)})
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
                assert request("/api/add", {"links": "\n".join(["https://youtu.be/aaaaaaaaaaa"] * 51), "root": str(root)})[0] == 400
                with patch.object(ui_server.sys, "platform", "darwin"):
                    with patch.object(ui_server.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=str(root) + "\n", stderr="")):
                        assert json.loads(request("/api/choose-folder", {})[1])["root"] == str(root)
                    with patch.object(ui_server.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="User canceled (-128)")):
                        assert json.loads(request("/api/choose-folder", {})[1])["root"] is None
                    with patch.object(ui_server.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 120)):
                        code, body, _ = request("/api/choose-folder", {})
                        assert code == 400 and "시간이 지났어요" in json.loads(body)["error"]

                code, body, _ = request("/api/add", {"links": "https://youtu.be/aaaaaaaaaaa\nhttps://youtube.com/watch?v=aaaaaaaaaaa&list=ignore\nhttps://evil.example/\nhttps://youtu.be/bbbbbbbbbbb\nhttps://youtu.be/ccccccccccc", "root": str(root)})
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
                failed_id = state["jobs"][1]["id"]
                assert request("/api/retry", {"id": failed_id})[0] == 200
                wait_idle()
                assert server.batch.snapshot()["jobs"][1]["status"] == "saved"
                assert request("/api/retry", {"id": []})[0] == 400
                assert request("/api/clear", {})[0] == 200
                assert server.batch.snapshot()["jobs"] == []
                assert len(list(root.rglob("*.mp3"))) == 3, "Clearing history must not delete audio"

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
    print("PASS: local HTTP auth, origin/Host checks, static-file boundaries, mixed links, sequential queue, failure isolation, retry, cancellation, non-destructive history clearing.")


if __name__ == "__main__":
    check()
