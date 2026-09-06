"""Offline smoke check: generated audio, real FFmpeg/ID3/CSV, no network."""

import csv
import errno
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from mutagen.id3 import ID3

import music_pipeline as pipeline
import youtube_import as importer


def audio_hash(path):
    return subprocess.check_output(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "hash", "-"],
        text=True,
    ).strip()


def check():
    url = "https://www.youtube.com/watch?v=BaW_jenozKc"
    assert importer.youtube_url("https://youtu.be/BaW_jenozKc?list=IGNORE&t=12") == (url, "BaW_jenozKc")
    assert importer.youtube_url("https://www.youtube.com/shorts/BaW_jenozKc")[1] == "BaW_jenozKc"
    for invalid in ("file:///etc/passwd", "https://youtube.com.evil.example/watch?v=BaW_jenozKc", "https://youtube.com/playlist?list=x"):
        try:
            importer.youtube_url(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted invalid URL: {invalid}")
    info = {"id": "BaW_jenozKc", "title": "가수 - Song (DJ Remix) [Official Audio]", "artist": "Original Artist",
            "track": "Song", "album": "Original Album", "channel": "Repost channel"}
    tags = importer.metadata_from_video(info, url, {})
    assert tags["artist"] == "가수" and tags["title"] == "Song (DJ Remix)" and tags["album"] == ""
    assert url in tags["comment"] and "Repost channel" in tags["comment"]
    assert importer.metadata_from_video({**info, "title": "Artist - Song"}, url, {})["album"] == "Original Album"
    assert importer.metadata_from_video({**info, "title": "Artist - Song"}, url, {"title": "Song (Remix)"})["album"] == ""
    assert importer.metadata_from_video({"title": "Unknown song", "uploader": "Not the artist"}, url, {})["artist"] == ""
    assert importer.metadata_from_video(info, url, {"artist": "DJ", "genre": "DnB"})["artist"] == "DJ"
    assert importer.reject_live({"is_live": True})
    assert not importer.reject_live({"live_status": "was_live"})
    assert len(importer.filename_for({"artist": "가" * 200, "title": "../../Track"}, "BaW_jenozKc").encode()) < 255

    with tempfile.TemporaryDirectory(prefix="youtube-dj-test-") as work:
        root = Path(work).resolve()

        def fake_download(_url, stage):
            source = stage / "source.m4a"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
                 "-c:a", "aac", str(source)], check=True,
            )
            return info, source

        with patch.object(importer, "download_source", side_effect=fake_download) as download:
            path = importer.import_video(url, root, {})
            assert path.parent == root / "tracks"
            id3 = ID3(path)
            assert id3.version == (2, 3, 0)
            assert str(id3["TPE1"]) == "가수" and str(id3["TIT2"]) == "Song (DJ Remix)"
            assert str(id3["TXXX:YouTube ID"]) == "BaW_jenozKc"
            assert id3["WOAS"].url == url
            assert (root / "_sources" / "BaW_jenozKc" / "source.m4a").exists()
            assert importer.import_video(url, root, {}) == path
            assert download.call_count == 1

        before = audio_hash(path)
        with patch.object(importer.os, "link", side_effect=OSError(errno.ENOTSUP, "No hardlinks")):
            copy = root / ".usb-copy.mp3"
            importer.publish_mp3(path, copy)
            assert audio_hash(copy) == before
            try:
                importer.publish_mp3(path, copy)
            except FileExistsError:
                pass
            else:
                raise AssertionError("An existing destination was overwritten")
        # An MP3 source archive must never become a second library track.
        shutil.copy2(path, root / "_sources" / "BaW_jenozKc" / "source.mp3")
        assert pipeline.audio_files(root) == [path]
        rows = pipeline.load_manifest(root / "metadata.csv")
        assert rows[path.name]["artist"] == "가수" and rows[path.name]["album"] == ""
        rows[path.name]["genre"] = "Drum & Bass"
        with (root / "metadata.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=pipeline.FIELDS)
            writer.writeheader()
            writer.writerows(rows.values())
        pipeline.write_manifest(root)
        assert pipeline.load_manifest(root / "metadata.csv")[path.name]["genre"] == "Drum & Bass"
        pipeline.apply(root, overwrite=False)
        assert pipeline.probe_tags(path)["genre"] == "Drum & Bass"
        assert audio_hash(path) == before
        assert url in pipeline.probe_tags(path)["comment"]

        def unknown_download(_url, stage):
            source = stage / "source.mp3"
            shutil.copy2(path, source)
            return {"id": "aqz-KE-bpKQ", "title": "Unknown song", "channel": "Uploader"}, source

        with patch.object(importer, "download_source", side_effect=unknown_download):
            unknown = importer.import_video("https://youtu.be/aqz-KE-bpKQ", root, {})
        assert unknown.parent == root / "_inbox"
        assert not pipeline.load_manifest(root / "metadata.csv")[unknown.name]["artist"]
        assert audio_hash(unknown) == before

        with patch.object(importer, "download_source", side_effect=RuntimeError("Simulated download failure")):
            try:
                importer.import_video("https://youtu.be/abcdefghijk", root, {})
            except RuntimeError:
                pass
            else:
                raise AssertionError("A failed download appeared successful")
        assert not list(root.glob(".youtube-*"))
        assert len(pipeline.audio_files(root)) == 2
        assert audio_hash(path) == before
    print("PASS: URL validation, remix metadata, Unicode filenames, ID3, audio conversion/copy, duplicate protection, CSV review, inbox routing, failure cleanup.")


if __name__ == "__main__":
    check()
