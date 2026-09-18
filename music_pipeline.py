#!/usr/bin/env python3
"""Review and apply local MP3 metadata without re-encoding audio."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from mutagen.id3 import COMM, TALB, TCON, TIT2, TPE1, ID3, ID3NoHeaderError


DEFAULT_ROOT = Path.home() / "Music" / "minsmix"
MANIFEST_NAME = "metadata.csv"
FIELDS = ("filename", "relative_path", "artist", "title", "album", "genre", "comment")


def audio_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == ".mp3"
        and not any(part.startswith(".") or part == "_sources" for part in path.relative_to(root).parts)
    )


@contextmanager
def library_lock(root: Path):
    """Serialize imports and CSV edits so two commands cannot lose a row."""
    with (root / ".music.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another import or metadata command is using this folder. Try again when it finishes.")
        yield


def parse_name(path: Path) -> tuple[str, str]:
    parts = path.stem.split(" - ", 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", "")


def infer_row(path: Path, root: Path) -> dict[str, str]:
    artist, title = parse_name(path)
    tags = probe_tags(path) if path.exists() else {}
    folders = {part.lower() for part in path.relative_to(root).parts[:-1]}
    return {
        "filename": path.name,
        "relative_path": str(path.relative_to(root)),
        "artist": tags.get("artist", "" if tags.get("title") else artist),
        "title": tags.get("title") or title,
        "album": tags.get("album", ""),
        "genre": tags.get("genre") or ("Drum & Bass" if "dnb" in folders else ""),
        "comment": tags.get("comment", ""),
    }


def load_manifest(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {row["filename"]: row for row in csv.DictReader(handle)}


def write_manifest(root: Path, overrides: dict[str, dict[str, str]] | None = None) -> Path:
    manifest = root / MANIFEST_NAME
    existing = load_manifest(manifest)
    existing.update(overrides or {})
    files = audio_files(root)
    duplicates = sorted(name for name, count in Counter(path.name for path in files).items() if count > 1)
    if duplicates:
        raise SystemExit(f"Duplicate filenames need manual review: {', '.join(duplicates)}")

    rows: list[dict[str, str]] = []
    for path in files:
        row = existing[path.name] if path.name in existing else infer_row(path, root)
        row["relative_path"] = str(path.relative_to(root))
        rows.append({field: row.get(field, "") for field in FIELDS})

    fd, temp_name = tempfile.mkstemp(prefix="metadata-", suffix=".csv", dir=root)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, manifest)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    print(f"Wrote {len(rows)} rows to {manifest}")
    return manifest


def probe_tags(path: Path) -> dict[str, str]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    tags = json.loads(result.stdout).get("format", {}).get("tags", {})
    return {key.lower(): str(value) for key, value in tags.items()}


def write_tags(path: Path, row: dict[str, str], overwrite: bool, *, clear_empty: bool = False) -> bool:
    current = probe_tags(path)
    desired = {field: row[field].strip() for field in ("artist", "title", "album", "genre", "comment")}
    updates = {
        field: value
        for field, value in desired.items()
        if (value or clear_empty) and (overwrite or not current.get(field)) and current.get(field, "") != value
    }
    if not updates:
        return False

    fd, temp_name = tempfile.mkstemp(prefix=".tag-", suffix=".mp3", dir=path.parent)
    os.close(fd)
    try:
        shutil.copy2(path, temp_name)
        try:
            id3 = ID3(temp_name)
        except ID3NoHeaderError:
            id3 = ID3()
        for field, frame in (("artist", TPE1), ("title", TIT2), ("album", TALB), ("genre", TCON)):
            if field in updates:
                id3.delall(frame.__name__)
                if updates[field]:
                    id3.add(frame(encoding=1, text=updates[field]))
        if "comment" in updates:
            # Preserve other described comments, artwork, cue frames, and source identifiers.
            for key in list(id3.keys()):
                if key.startswith("COMM:") and not id3[key].desc:
                    del id3[key]
            if updates["comment"]:
                id3.add(COMM(encoding=1, lang="eng", desc="", text=updates["comment"]))
        id3.save(temp_name, v2_version=3)
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return True


EDIT_FIELDS = ("artist", "title", "album", "genre")


def source_identity(tags: dict[str, str]) -> tuple[str, str]:
    platform = tags.get("source platform", "").casefold()
    source_id = tags.get("source id", "")
    if not source_id and tags.get("youtube id"):
        return "youtube", tags["youtube id"]
    if not source_id and tags.get("soundcloud id"):
        return "soundcloud", tags["soundcloud id"]
    return platform, source_id


def resolve_track(root: Path, filename: str, source_id: str = "", source_platform: str = "") -> Path:
    """Resolve only library MP3s, recovering a renamed download by its embedded source ID."""
    if (not isinstance(filename, str) or not filename or not isinstance(source_id, str)
            or not isinstance(source_platform, str)):
        raise ValueError("편집할 곡을 선택해 주세요.")
    if source_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source_id):
        raise ValueError("원본 곡 식별자가 올바르지 않아요.")
    if source_platform not in {"", "youtube", "soundcloud"}:
        raise ValueError("원본 서비스 식별자가 올바르지 않아요.")
    path = Path(filename)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("선택한 음악 폴더 안의 파일만 편집할 수 있어요.")
    relative = path.relative_to(root)
    if path.suffix.lower() != ".mp3" or any(p.startswith(".") or p == "_sources" for p in relative.parts):
        raise ValueError("라이브러리의 MP3 파일만 편집할 수 있어요.")
    if path.is_file():
        embedded_platform, embedded_id = source_identity(probe_tags(path))
        if not source_id or (embedded_id == source_id and (not source_platform or embedded_platform == source_platform)):
            return path
    if source_id:
        matches = []
        for candidate in audio_files(root):
            embedded_platform, embedded_id = source_identity(probe_tags(candidate))
            if (candidate.resolve().is_relative_to(root) and embedded_id == source_id
                    and (not source_platform or embedded_platform == source_platform)):
                matches.append(candidate)
        if len(matches) == 1:
            return matches[0]
    raise ValueError("파일이 이동되었거나 없어요. ‘라이브러리 불러오기’로 목록을 새로고침해 주세요.")


def track_review(root: Path, path: Path) -> dict:
    tags = probe_tags(path)
    manifest = load_manifest(root / MANIFEST_NAME)
    name = path.name
    platform, source_id = source_identity(tags)
    comment = tags.get("comment", "")
    source_match = re.search(r"(?:^| \| )Source: (https://\S+?)(?= \| |$)", comment)
    source_url = tags.get("source url", "") or (source_match.group(1) if source_match else "")
    if not source_url and platform == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", source_id):
        source_url = f"https://www.youtube.com/watch?v={source_id}"
    if name not in manifest and source_url:
        matches = [key for key, row in manifest.items() if f"Source: {source_url}" in row.get("comment", "")]
        if len(matches) == 1:
            name = matches[0]
    row = manifest.get(name, {})
    values = {field: row.get(field, tags.get(field, "")) for field in EDIT_FIELDS}
    stat = path.stat()
    revision = hashlib.sha256(json.dumps([str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size, tags, row], sort_keys=True).encode()).hexdigest()
    title_match = re.search(r"(?:Video|Track|Source) title: (.*?)(?= \| Metadata:|$)", comment)
    original_title = title_match.group(1) if title_match else ""
    pending = bool(row) and any(values[field] != tags.get(field, "") for field in EDIT_FIELDS)
    video_id = source_id if platform == "youtube" else ""
    return {"root": str(root), "path": str(path), "revision": revision, "tags": values,
            "source_id": source_id, "source_platform": platform, "video_id": video_id,
            "source_url": source_url, "video_title": original_title, "csv_pending": pending,
            "status": "review" if path.relative_to(root).parts[0] == "_inbox" or pending or not tags.get("artist") or not tags.get("title") else "saved"}


def save_review(root: Path, path: Path, revision: str, values: dict) -> dict:
    """Caller holds library_lock. Back up and change exactly one file and its CSV row."""
    if not isinstance(values, dict) or set(values) != set(EDIT_FIELDS):
        raise ValueError("아티스트·제목·앨범·장르를 확인해 주세요.")
    if any(not isinstance(value, str) or len(value) > 512 or any(ord(char) < 32 for char in value) for value in values.values()):
        raise ValueError("태그는 줄바꿈 없이 512자 이내로 입력해 주세요.")
    values = {key: value.strip() for key, value in values.items()}
    if not values["artist"] or not values["title"]:
        raise ValueError("아티스트와 제목은 필수예요. 앨범·장르는 비워둬도 돼요.")
    current = track_review(root, path)
    if not isinstance(revision, str) or current["revision"] != revision:
        raise ValueError("파일이나 메타데이터 표가 바뀌었어요. 편집창을 닫고 다시 열어 최신 값을 확인해 주세요.")
    files = audio_files(root)
    if len({p.name for p in files}) != len(files):
        raise ValueError("같은 파일명이 여러 개 있어요. 이름을 구분한 뒤 다시 저장해 주세요.")
    destination = root / "tracks" / path.name if path.relative_to(root).parts[0] == "_inbox" else path
    if not destination.parent.resolve().is_relative_to(root):
        raise ValueError("저장 위치가 선택한 음악 폴더 밖을 가리키고 있어요.")
    if destination != path and destination.exists():
        raise ValueError("tracks에 같은 이름의 파일이 있어요. 기존 파일을 덮어쓰지 않았어요.")
    comment = probe_tags(path).get("comment", "")
    comment = re.sub(r" \| Review: missing (?:artist(?:, title)?|title)(?= \| |$)", "", comment)
    comment = comment.replace("Metadata: needs review", "Metadata: reviewed UI")
    if "Reviewed: UI" not in comment:
        comment = (comment + " | Reviewed: UI").lstrip(" |")
    row = {**values, "comment": comment, "filename": destination.name,
           "relative_path": str(destination.relative_to(root))}
    backup_root = root / ".tag-backups"
    if (root / MANIFEST_NAME).is_symlink():
        raise ValueError("메타데이터 표가 폴더 밖의 파일과 연결돼 있어요. 실제 CSV 파일을 사용해 주세요.")
    if backup_root.is_symlink():
        raise ValueError("백업 폴더가 심볼릭 링크예요. 실제 폴더로 바꾼 뒤 저장해 주세요.")
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="review-", dir=backup_root))
    shutil.copy2(path, backup / path.name)
    manifest = root / MANIFEST_NAME
    if manifest.exists():
        shutil.copy2(manifest, backup / MANIFEST_NAME)
    moved = False
    try:
        write_tags(path, row, overwrite=True, clear_empty=True)
        if destination != path:
            destination.parent.mkdir(exist_ok=True)
            shutil.move(str(path), destination)
            moved = True
        write_manifest(root, {destination.name: row})
    except (Exception, SystemExit):
        if moved:
            shutil.move(str(destination), path)
        shutil.copy2(backup / path.name, path)
        if (backup / MANIFEST_NAME).exists():
            shutil.copy2(backup / MANIFEST_NAME, manifest)
        raise RuntimeError(f"저장하지 못해 이전 상태로 복원했어요. 백업: {backup}")
    return {**track_review(root, destination), "backup": str(backup), "previous_path": str(path)}


def apply(root: Path, overwrite: bool) -> None:
    manifest = root / MANIFEST_NAME
    rows = load_manifest(manifest)
    if not rows:
        raise SystemExit(f"No metadata rows found. Run scan first: {manifest}")

    paths = audio_files(root)
    files = {path.name: path for path in paths}
    if len(files) != len(paths):
        raise SystemExit("Duplicate MP3 filenames need manual review; no tags were changed.")
    tracks = root / "tracks"
    inbox = root / "_inbox"
    tracks.mkdir(exist_ok=True)
    inbox.mkdir(exist_ok=True)
    changed = moved = skipped = 0

    for filename, row in rows.items():
        path = files.get(filename)
        if path is None:
            print(f"SKIP missing: {filename}")
            skipped += 1
            continue
        if not row.get("artist", "").strip() or not row.get("title", "").strip():
            print(f"SKIP needs Artist/Title: {filename}")
            skipped += 1
            continue
        destination = tracks / filename
        if path != destination and destination.exists():
            raise SystemExit(f"Refusing to overwrite: {destination}")
        if write_tags(path, row, overwrite):
            changed += 1
        if path.parent != tracks:
            shutil.move(str(path), destination)
            moved += 1

    write_manifest(root)
    print(f"Tagged {changed}, moved {moved}, skipped {skipped}")


def self_test() -> None:
    root = Path("/music")
    normal = infer_row(root / "Artist - Track.mp3", root)
    remix = infer_row(root / "remix" / "DJ - Song (REMIX).mp3", root)
    dnb = infer_row(root / "dnb" / "Sub Focus - Track.mp3", root)
    assert (normal["artist"], normal["title"]) == ("Artist", "Track")
    assert parse_name(Path("No separator.mp3")) == ("", "")
    assert remix["title"] == "Song (REMIX)"
    assert remix["album"] == ""
    assert dnb["genre"] == "Drum & Bass"
    print("Self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Infer, review, and write MP3 metadata without re-encoding audio.")
    parser.add_argument("command", choices=("run", "scan", "apply", "test"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--overwrite", action="store_true", help="Replace non-empty tags with CSV values.")
    args = parser.parse_args()

    if args.command == "test":
        self_test()
        return
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Music folder not found: {root}")
    with library_lock(root):
        if args.command == "run":
            write_manifest(root)
            apply(root, args.overwrite)
        elif args.command == "scan":
            write_manifest(root)
        else:
            apply(root, args.overwrite)


if __name__ == "__main__":
    main()
