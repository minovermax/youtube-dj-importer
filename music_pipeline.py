#!/usr/bin/env python3
"""Review and apply local MP3 metadata without re-encoding audio."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


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


def write_manifest(root: Path) -> Path:
    manifest = root / MANIFEST_NAME
    existing = load_manifest(manifest)
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


def write_tags(path: Path, row: dict[str, str], overwrite: bool) -> bool:
    current = probe_tags(path)
    desired = {field: row[field].strip() for field in ("artist", "title", "album", "genre", "comment")}
    updates = {
        field: value
        for field, value in desired.items()
        if value and (overwrite or not current.get(field)) and current.get(field) != value
    }
    if not updates:
        return False

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".mp3", dir=path.parent)
    os.close(fd)
    Path(temp_name).unlink()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-map",
        "0",
        "-c",
        "copy",
        "-map_metadata",
        "0",
    ]
    for field, value in updates.items():
        command.extend(("-metadata", f"{field}={value}"))
    command.extend(("-id3v2_version", "3", temp_name))
    try:
        subprocess.run(command, check=True)
        if Path(temp_name).stat().st_size == 0:
            raise RuntimeError(f"ffmpeg created an empty file for {path.name}")
        shutil.copymode(path, temp_name)
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return True


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
