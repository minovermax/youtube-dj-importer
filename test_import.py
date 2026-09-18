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
    soundcloud_url = "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy"
    soundcloud = importer.parse_media_url(soundcloud_url + "?si=tracking&utm_source=clipboard")
    assert soundcloud.platform == "soundcloud" and soundcloud.url == soundcloud_url and soundcloud.source_id == ""
    assert importer.parse_media_url("https://on.soundcloud.com/AbC123").platform == "soundcloud"
    for invalid in ("file:///etc/passwd", "https://youtube.com.evil.example/watch?v=BaW_jenozKc", "https://youtube.com/playlist?list=x"):
        try:
            importer.youtube_url(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted invalid URL: {invalid}")
    for invalid in ("https://soundcloud.com/artist", "https://soundcloud.com/artist/sets/mixes",
                    "https://soundcloud.com/artist/tracks"):
        try:
            importer.parse_media_url(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted a non-track SoundCloud URL: {invalid}")
    info = {"id": "BaW_jenozKc", "title": "가수 - Song (DJ Remix) [Official Audio]", "artist": "Original Artist",
            "track": "Song", "album": "Original Album", "channel": "Repost channel"}
    tags = importer.metadata_from_video(info, url, {})
    assert tags["artist"] == "가수" and tags["title"] == "Song (DJ Remix)" and tags["album"] == ""
    assert url in tags["comment"] and "Repost channel" in tags["comment"]
    assert importer.metadata_from_video({**info, "title": "Artist - Song"}, url, {})["album"] == "Original Album"
    assert importer.metadata_from_video({**info, "title": "Artist - Song"}, url, {"title": "Song (Remix)"})["album"] == ""
    assert importer.metadata_from_video({"title": "Unknown song", "uploader": "Not the artist"}, url, {})["artist"] == ""
    assert importer.metadata_from_video(info, url, {"artist": "DJ", "genre": "DnB"})["artist"] == "DJ"
    soundcloud_info = {"id": "62986583", "title": "Lostin Powers - She so Heavy (SneakPreview)",
                       "track": "Lostin Powers - She so Heavy (SneakPreview)",
                       "uploader": "E.T. ExTerrestrial Music", "genres": ["Electronic"]}
    soundcloud_tags = importer.metadata_from_video(soundcloud_info, soundcloud_url, {}, "soundcloud")
    assert soundcloud_tags["artist"] == "Lostin Powers"
    assert soundcloud_tags["title"] == "She so Heavy (SneakPreview)"
    assert soundcloud_tags["genre"] == "Electronic" and "Track title:" in soundcloud_tags["comment"]
    assert importer.reject_live({"is_live": True})
    assert not importer.reject_live({"live_status": "was_live"})
    assert importer.browser_cookie_source("chrome") == ("chrome", None, None, None)
    assert importer.browser_cookie_source(None) is None
    cover_url = importer.music_cover_url(
        '<meta property="og:image" content="https://yt3.googleusercontent.com/example=w544-h544">'
    )
    assert cover_url == "https://yt3.googleusercontent.com/example=s0"
    try:
        importer.music_cover_url('<meta property="og:image" content="https://example.com/not-trusted.jpg">')
    except ValueError:
        pass
    else:
        raise AssertionError("Accepted an untrusted artwork host")
    try:
        importer.browser_cookie_source("unknown")
    except ValueError:
        pass
    else:
        raise AssertionError("Accepted an unsupported cookie browser")
    assert len(importer.filename_for({"artist": "가" * 200, "title": "../../Track"}).encode()) < 255

    with tempfile.TemporaryDirectory(prefix="youtube-dj-test-") as work:
        root = Path(work).resolve()

        def fake_download(_url, stage, *_args, **_kwargs):
            source = stage / "source.m4a"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
                 "-c:a", "aac", str(source)], check=True,
            )
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64",
                 "-frames:v", "1", str(stage / "source.png")], check=True,
            )
            return info, source

        def fake_music_cover(_video_id, stage):
            cover = stage / "music-cover.png"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=96x96",
                 "-frames:v", "1", str(cover)], check=True,
            )
            return cover

        with (patch.object(importer, "download_source", side_effect=fake_download) as download,
              patch.object(importer, "download_music_cover", side_effect=fake_music_cover)):
            path = importer.import_video(url, root, {})
            assert path.parent == root / "tracks"
            assert path.name == "가수 - Song (DJ Remix).mp3"
            id3 = ID3(path)
            assert id3.version == (2, 3, 0)
            assert str(id3["TPE1"]) == "가수" and str(id3["TIT2"]) == "Song (DJ Remix)"
            assert str(id3["TXXX:YouTube ID"]) == "BaW_jenozKc"
            assert id3["WOAS"].url == url
            artwork = root / "artwork" / "가수 - Song (DJ Remix).jpg"
            assert artwork.exists()
            covers = id3.getall("APIC")
            assert len(covers) == 1 and covers[0].mime == "image/jpeg" and covers[0].type == 3
            assert covers[0].data == artwork.read_bytes()
            assert (root / "_sources" / "BaW_jenozKc" / "source.m4a").exists()
            assert (root / "_sources" / "BaW_jenozKc" / "source.png").exists()
            assert (root / "_sources" / "BaW_jenozKc" / "music-cover.png").exists()
            assert importer.import_video(url, root, {}) == path
            assert download.call_count == 1
            renamed = path.with_name("Manually renamed.mp3")
            path.rename(renamed)
            assert importer.import_video(url, root, {}) == renamed
            renamed.rename(path)
            assert download.call_count == 1

        with (patch.object(importer, "download_source", side_effect=fake_download),
              patch.object(importer, "download_music_cover", side_effect=fake_music_cover)):
            same_title = importer.import_video("https://youtu.be/bbbbbbbbbbb", root, {})
            assert same_title.name == "가수 - Song (DJ Remix) (2).mp3"
            assert (root / "artwork" / "가수 - Song (DJ Remix) (2).jpg").exists()
            assert str(ID3(path)["TXXX:YouTube ID"]) == "BaW_jenozKc"

        before = audio_hash(path)
        with patch.object(importer.os, "link", side_effect=OSError(errno.ENOTSUP, "No hardlinks")):
            copy = root / ".usb-copy.mp3"
            importer.publish_file(path, copy)
            assert audio_hash(copy) == before
            try:
                importer.publish_file(path, copy)
            except FileExistsError:
                pass
            else:
                raise AssertionError("An existing destination was overwritten")
        # An MP3 source archive must never become a second library track.
        shutil.copy2(path, root / "_sources" / "BaW_jenozKc" / "source.mp3")
        assert set(pipeline.audio_files(root)) == {path, same_title}
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

        def fake_soundcloud_download(_url, stage, *_args, **_kwargs):
            source = stage / "source.m4a"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=550:duration=0.2",
                 "-c:a", "aac", str(source)], check=True,
            )
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=120x120",
                 "-frames:v", "1", str(stage / "source.jpg")], check=True,
            )
            return {**soundcloud_info, "webpage_url": soundcloud_url}, source

        with patch.object(importer, "download_source", side_effect=fake_soundcloud_download) as soundcloud_download:
            soundcloud_path = importer.import_video(soundcloud_url, root, {})
            assert soundcloud_path.name == "Lostin Powers - She so Heavy (SneakPreview).mp3"
            soundcloud_id3 = ID3(soundcloud_path)
            assert str(soundcloud_id3["TXXX:Source Platform"]) == "soundcloud"
            assert str(soundcloud_id3["TXXX:Source ID"]) == "62986583"
            assert str(soundcloud_id3["TXXX:SoundCloud ID"]) == "62986583"
            assert str(soundcloud_id3["TCON"]) == "Electronic"
            assert (root / "artwork" / "Lostin Powers - She so Heavy (SneakPreview).jpg").exists()
            assert (root / "_sources" / "soundcloud-62986583" / "source.m4a").exists()
            assert importer.import_video(soundcloud_url + "?si=another", root, {}) == soundcloud_path
            assert soundcloud_download.call_count == 1

        def unknown_download(_url, stage, *_args, **_kwargs):
            source = stage / "source.mp3"
            shutil.copy2(path, source)
            return {"id": "aqz-KE-bpKQ", "title": "Unknown song", "channel": "Uploader"}, source

        with (patch.object(importer, "download_source", side_effect=unknown_download),
              patch.object(importer, "download_music_cover", return_value=None)):
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
        assert not list(root.glob(".import-*"))
        assert len(pipeline.audio_files(root)) == 4
        assert audio_hash(path) == before
    print("PASS: YouTube/SoundCloud URL validation, remix metadata, artwork, Unicode filenames, source IDs, audio conversion/copy, duplicate protection, CSV review, inbox routing, failure cleanup.")


if __name__ == "__main__":
    check()
