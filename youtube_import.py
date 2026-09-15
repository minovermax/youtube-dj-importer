#!/usr/bin/env python3
"""One YouTube URL -> best available source audio -> tagged DJ-library MP3."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mutagen.id3 import COMM, TALB, TCON, TIT2, TPE1, TXXX, WOAS, ID3, ID3NoHeaderError
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from music_pipeline import DEFAULT_ROOT, audio_files, library_lock, write_manifest


def youtube_url(value: str) -> tuple[str, str]:
    """Accept only a single YouTube video; discard playlist and tracking parameters."""
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("Paste a full https:// YouTube video link.")
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/")
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([\w-]{11})/?", parsed.path)
            video_id = match[1] if match else ""
    else:
        raise ValueError("Only youtube.com and youtu.be video links are supported.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("This is not a single-video link. Paste a watch, Shorts, live-video, or youtu.be URL.")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


def clean(value) -> str:
    return " ".join(str(value or "").split())


def metadata_from_video(info: dict, url: str, overrides: dict) -> dict[str, str]:
    raw_title = clean(info.get("title"))
    title = raw_title or clean(info.get("track"))
    # ponytail: title parsing is a heuristic; ambiguous credits need human review, not a database guess.
    suffix = r"\s*[\[(](?:official (?:music )?(?:video|audio)|lyric(?:s)? video|lyrics|visuali[sz]er|HD|4K)[\])]\s*$"
    title = re.sub(suffix, "", title, flags=re.I).strip()
    parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    artist = clean(info.get("artist"))
    method = "YouTube fields" if artist else "needs review"
    if len(parts) == 2 and all(parts):
        artist, title = parts
        method = "video title (inferred)"
    tags = {"artist": artist, "title": title, "album": "", "genre": ""}
    tags.update({key: clean(value) for key, value in overrides.items() if key in tags and value is not None})
    # Content-ID can identify the original song underneath a remix. Do not borrow its album.
    if overrides.get("album") is None and tags["title"] and clean(info.get("track")).casefold() == tags["title"].casefold():
        tags["album"] = clean(info.get("album"))
    if any(value is not None for value in overrides.values()):
        method += "; manual override"
    notes = [f"Source: {url}", f"Video title: {raw_title}", f"Metadata: {method}"]
    channel = clean(info.get("channel") or info.get("uploader"))
    if channel:
        notes.append(f"Channel: {channel}")
    missing = [field for field in ("artist", "title") if not tags[field]]
    if missing:
        notes.append(f"Review: missing {', '.join(missing)}")
    tags["comment"] = " | ".join(notes)
    return tags


def filename_for(tags: dict) -> str:
    name = f"{tags['artist']} - {tags['title']}" if tags["artist"] else tags["title"] or "Untitled"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip(" .") or "Untitled"
    # Leave room for a collision counter and extension within filesystem byte limits.
    name = name.encode("utf-8")[:200].decode("utf-8", errors="ignore").rstrip(" .")
    return f"{name}.mp3"


def embed_tags(path: Path, tags: dict, url: str, video_id: str) -> None:
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    for frame, field in ((TPE1, "artist"), (TIT2, "title"), (TALB, "album"), (TCON, "genre")):
        id3.delall(frame.__name__)
        if tags[field]:
            id3.add(frame(encoding=1, text=tags[field]))
    id3.delall("COMM")
    id3.add(COMM(encoding=1, lang="eng", desc="", text=tags["comment"]))
    id3.add(WOAS(url=url))
    id3.add(TXXX(encoding=1, desc="YouTube ID", text=video_id))
    id3.save(path, v2_version=3)


def reject_live(info: dict, *, incomplete: bool = False):
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        return "Live/upcoming/processing streams are unsupported; use a finished video."
    return None


COOKIE_BROWSERS = {"brave", "chrome", "edge", "firefox", "safari"}


def browser_cookie_source(value: str | None):
    if value is not None and (not isinstance(value, str) or value not in COOKIE_BROWSERS):
        raise ValueError("Choose a supported browser for YouTube login: Chrome, Firefox, Safari, Edge, or Brave.")
    return (value, None, None, None) if value else None


def download_source(url: str, stage: Path, on_progress=None, browser_cookies: str | None = None) -> tuple[dict, Path]:
    runtime = next((name for name in ("deno", "node") if shutil.which(name)), None)
    if runtime is None:
        raise RuntimeError("YouTube needs Deno or Node.js. Install Node 22+ (or Deno) and try again.")
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(stage).replace("%", "%%") + "/source.%(ext)s",
        "noplaylist": True,
        "match_filter": reject_live,
        "js_runtimes": {runtime: {"path": shutil.which(runtime)}},
        "socket_timeout": 30,
    }
    if cookies := browser_cookie_source(browser_cookies):
        options["cookiesfrombrowser"] = cookies
    if on_progress:
        def progress(data):
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            percent = min(100, round(data.get("downloaded_bytes", 0) / total * 100)) if total else None
            on_progress({"status": "downloading", "progress": percent,
                         "title": clean(data.get("info_dict", {}).get("title"))})
        options.update(progress_hooks=[progress], quiet=True, noprogress=True)
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
            if not info or info.get("_type") in {"playlist", "multi_video"} or reject_live(info):
                raise RuntimeError("No finished single video could be downloaded.")
            source = Path(downloader.prepare_filename(info))
    except DownloadError as error:
        if "Sign in to confirm your age" in str(error):
            if browser_cookies:
                raise RuntimeError(f"{browser_cookies.title()}에서 YouTube 로그인과 연령 확인을 마친 뒤 다시 시도해 주세요.") from error
            raise RuntimeError("연령 제한 영상이에요. UI의 ‘YouTube 로그인’에서 로그인된 브라우저를 선택해 다시 시도해 주세요.") from error
        raise
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("The audio download did not complete.")
    return info, source


def make_mp3(source: Path, destination: Path) -> None:
    codec = ["-c:a", "copy"] if source.suffix.lower() == ".mp3" else ["-c:a", "libmp3lame", "-b:a", "320k"]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror", "-nostdin", "-n", "-i", str(source),
         "-map", "0:a:0", "-vn", "-map_metadata", "-1", *codec, "-id3v2_version", "3", str(destination)],
        check=True,
    )


def publish_mp3(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)  # Atomic and refuses to replace an existing file.
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP}:
            raise
        # FAT/exFAT USB drives do not support hard links. Exclusive creation still prevents overwrites.
        output = destination.open("xb")
        try:
            with output, source.open("rb") as original:
                shutil.copyfileobj(original, output)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def import_video(value: str, root: Path, overrides: dict, *, on_progress=None, browser_cookies: str | None = None) -> Path:
    url, video_id = youtube_url(value)
    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            raise RuntimeError(f"Missing {binary}. Install FFmpeg first (macOS: brew install ffmpeg).")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with library_lock(root):
        existing = None
        for path in audio_files(root):
            try:
                embedded_id = str(ID3(path).get("TXXX:YouTube ID", ""))
            except ID3NoHeaderError:
                embedded_id = ""
            if embedded_id == video_id or not embedded_id and path.name.endswith(f"[{video_id}].mp3"):
                existing = path
                break
        if existing:
            if on_progress:
                on_progress({"status": "skipped", "path": str(existing), "title": existing.stem})
            print(f"Already downloaded: {existing}\nTo correct tags, open the UI tag editor or use metadata.csv.")
            return existing
        archive = root / "_sources" / video_id
        if archive.exists():
            raise RuntimeError(f"A source archive already exists at {archive}. Inspect it before retrying; nothing was overwritten.")
        with tempfile.TemporaryDirectory(prefix=".youtube-", dir=root) as work:
            stage = Path(work)
            info, source = (download_source(url, stage, on_progress, browser_cookies)
                            if on_progress or browser_cookies else download_source(url, stage))
            tags = metadata_from_video(info, url, overrides)
            if on_progress:
                on_progress({"status": "tagging", "progress": None, "title": tags["title"], "artist": tags["artist"]})
            mp3 = stage / "track.mp3"
            make_mp3(source, mp3)
            embed_tags(mp3, tags, url, video_id)
            folder = "tracks" if tags["artist"] and tags["title"] else "_inbox"
            destination = root / folder / filename_for(tags)
            destination.parent.mkdir(exist_ok=True)
            # CSV rows use basenames, so distinguish matching titles across both folders.
            names = {p.name.casefold() for p in audio_files(root)}
            stem, counter = destination.stem, 2
            while destination.name.casefold() in names or destination.exists():
                destination = destination.with_name(f"{stem} ({counter}).mp3")
                counter += 1
            provenance = {key: info.get(key) for key in (
                "id", "title", "artist", "track", "album", "channel", "uploader", "upload_date",
                "format_id", "ext", "acodec", "abr", "asr",
            )}
            provenance.update(source_url=url, applied_tags=tags, source_filename=source.name)
            with (stage / "metadata.json").open("w", encoding="utf-8") as handle:
                json.dump(provenance, handle, ensure_ascii=False, indent=2)
            archive.parent.mkdir(exist_ok=True)
            archive.mkdir()  # Never replace an existing download, even after an interrupted import.
            shutil.move(str(source), archive / source.name)
            shutil.move(str(stage / "metadata.json"), archive / "metadata.json")
            publish_mp3(mp3, destination)
            write_manifest(root)
        if on_progress:
            on_progress({"status": "review" if folder == "_inbox" else "saved", "progress": 100,
                         "path": str(destination), "title": tags["title"], "artist": tags["artist"]})
        print(f"\nSaved: {destination}\nArtist: {tags['artist'] or '(needs review)'}\nTitle: {tags['title'] or '(needs review)'}")
        print(f"Original source preserved: {archive}")
        if folder == "_inbox":
            print("Needs review: fill Artist/Title in the UI tag editor, or use metadata.csv and music_pipeline.py apply.")
        return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", help="Single YouTube video URL; prompts if omitted.")
    parser.add_argument("--root", "--output", type=Path, default=DEFAULT_ROOT, help=f"Library/download directory (default: {DEFAULT_ROOT}).")
    for field in ("artist", "title", "album", "genre"):
        parser.add_argument(f"--{field}", help=f"Override the {field} tag.")
    parser.add_argument("--open", action="store_true", help="Reveal the downloaded file in Finder (macOS).")
    parser.add_argument("--cookies-from-browser", choices=sorted(COOKIE_BROWSERS), metavar="BROWSER",
                        help="Use a signed-in browser only for account-gated videos (chrome, firefox, safari, edge, brave).")
    args = parser.parse_args()
    try:
        url = args.url or input("YouTube link: ").strip()
        print(f"Download directory: {args.root.expanduser().resolve()}")
        destination = import_video(url, args.root, {field: getattr(args, field) for field in ("artist", "title", "album", "genre")},
                                   browser_cookies=args.cookies_from_browser)
        if args.open and sys.platform == "darwin":
            subprocess.run(["open", "-R", str(destination)], check=True)
    except (ValueError, RuntimeError, OSError, DownloadError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"\nImport failed: {error}\n")
    except (KeyboardInterrupt, EOFError):
        parser.exit(130, "\nCancelled. No existing tracks were overwritten.\n")


if __name__ == "__main__":
    main()
