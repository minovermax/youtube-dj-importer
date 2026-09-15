# YouTube DJ Importer

Paste one or several YouTube video links. Get tagged MP3s in a folder you choose, with the original downloaded sources preserved separately. Designed for a small local rekordbox library, including remixes that are not in music databases.

Use only for audio you own or have permission to download. This tool does not grant download or public-performance rights.

## Setup

Requires Python 3.10+, FFmpeg and Node.js 22+ (or Deno). macOS and Linux are supported; the double-click launcher is macOS-only.

```sh
git clone https://github.com/minovermax/youtube-dj-importer.git
cd youtube-dj-importer

# macOS: install missing system dependencies
brew install python ffmpeg node

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Download

### Small batch UI

On macOS, double-click **Open minsmix.command**. A Korean-language browser UI opens locally at `http://127.0.0.1:8765`:

1. Paste up to 50 video links, one per line.
2. Choose a download folder with the native folder picker, or type its path.
3. Start the batch. Track each download, open result folders, and retry failed items.

For an age-restricted video, first sign in to YouTube and complete age verification in your browser. Then choose that browser under **YouTube 로그인** before adding the link or pressing **다시 시도**. Leave it at **사용 안 함** for ordinary videos. Browser cookies are read locally for that download and are not exported or saved by this app. Use an account session only when necessary; authenticated downloading can put the account at risk if YouTube flags the activity.

Links run sequentially; one failure does not stop the others. Duplicate links in the queue are skipped. You can add more links while a batch runs, cancel waiting items without interrupting the current download, or clear finished history without deleting files. Unrecognized artist information goes to `_inbox` for review in the UI.

The UI requires no extra packages, account, or hosted server. It binds only to `127.0.0.1`, checks request Host/Origin and a per-session token, and never serves music files over HTTP. Keep the launcher/terminal window open while downloading. Closing the browser does not stop the queue. Ctrl+C cancels waiting items and waits for the current import to finish before exiting. Queue history is in memory and resets when the server restarts; downloaded files remain on disk. Your last-used folder is stored only in your browser.

```sh
.venv/bin/python ui_server.py
# Custom starting folder / alternate port / no automatic browser launch
.venv/bin/python ui_server.py --root '/path/to/DJ Music' --port 8766 --no-browser
```

Launching the UI again reopens the existing instance. If another program occupies the default port, choose another port. Existing library tracks load automatically; use **라이브러리 불러오기** to refresh them or load a different selected folder (up to 500 tracks).

### Single-link launcher and CLI

On macOS, double-click **Download YouTube.command**. Choose a download folder or press Enter for `~/Music/minsmix`, then paste a link. Finder reveals the finished MP3. Run the launcher from this repository; do not move it away from the Python files.

Or use the command line:

```sh
.venv/bin/python youtube_import.py 'https://www.youtube.com/watch?v=VIDEO_ID'

# Choose any library/download folder
.venv/bin/python youtube_import.py 'YOUTUBE_URL' --output '/path/to/DJ Music'

# Account-gated video (sign in to YouTube in Chrome first)
.venv/bin/python youtube_import.py 'YOUTUBE_URL' --cookies-from-browser chrome

# Correct an ambiguous title at download time
.venv/bin/python youtube_import.py 'YOUTUBE_URL' \
  --artist 'Artist Name' --title 'Song (DJ Remix)' --genre 'Drum & Bass'
```

`--root` is an alias for `--output`. `--album` is also available. Omit the URL to paste it at a prompt. Add `--open` to reveal the result in Finder. Playlist parameters are discarded; only one video is downloaded. Live/upcoming streams are rejected. Repeating the same video ID skips the download; it does not retag the existing file.

## What gets saved

```text
your-download-folder/
  tracks/             tagged MP3s with Artist and Title
  _inbox/             MP3s missing Artist or Title, for review
  _sources/VIDEO_ID/  original source audio and provenance JSON
  metadata.csv        editable review sheet
```

- **Artist / Title:** parsed from `Artist - Title`, retaining remix/edit names. Otherwise Artist comes from YouTube's structured artist field and Title from the video title. The channel/uploader is never used as the artist fallback. Title parsing is a heuristic, not verified song identification.
- **Album:** only taken from a structured YouTube album field when its track title matches the selected title. Unknown albums stay blank. No invented album or release year.
- **Genre:** blank unless provided explicitly; no audio-based genre guessing.
- **Comment:** source URL, original video title, channel, metadata provenance, and any missing-field warning. Source URL and video ID also get their own ID3 fields.

Filenames are `Artist - Title.mp3`, without a YouTube ID suffix. Different videos with the same name get a numeric suffix such as `(2)`. Duplicate downloads are recognized by the embedded YouTube ID, even after you rename a file. Legacy ID-suffixed filenames are also recognized.

The MP3 carries ID3v2.3 tags; metadata is not just a sidecar. Upload date stays in provenance JSON, not the release-year tag. Artwork, BPM, key detection, fingerprint lookup, and direct rekordbox-database changes are intentionally out of scope. Use rekordbox analysis for BPM/key and check beatgrids by ear.

Quality: yt-dlp selects the best available audio stream. Non-MP3 sources are encoded once to 320 kbps MP3 for compatibility; an MP3 source is copied without re-encoding. **320 kbps does not restore detail lost by YouTube.** The original stream remains in `_sources` if you want it later. Prefer a creator's original WAV/AIFF/FLAC download for performance use when available. Browser cookies are not accessed unless you explicitly select a browser for an account-gated download.

## Review or correct metadata

In the UI, click **태그 확인하기** (or **태그 수정** on a saved track):

1. Compare the original video title/link with the proposed tags. Use the performing artist, not necessarily the uploader; keep remix/edit credits in the title.
2. Enter Artist and Title. Album and Genre are optional; clearing them deliberately removes those tags.
3. Click **확인하고 저장**. The MP3 and its CSV row are updated together, and reviewed `_inbox` files move to `tracks` without changing their basename.

Audio is not re-encoded. Source identifiers, artwork and unrelated cue frames are preserved. Each UI save backs up the original MP3 and CSV under `.tag-backups/`; failures restore them. If the file or CSV changed while the editor was open, reopen the editor before saving. During an import, wait until the library is free and retry; your draft stays in the editor. Existing CSV draft values appear in the editor with a notice.

The CSV workflow remains available for bulk edits. Edit `metadata.csv`, keeping filenames unchanged, then run:

```sh
# Fill empty tags; move reviewed files from _inbox to tracks
.venv/bin/python music_pipeline.py apply

# Apply deliberate corrections to already-filled tags
.venv/bin/python music_pipeline.py apply --overwrite

# Refresh the sheet after adding local MP3s
.venv/bin/python music_pipeline.py scan
```

For a custom library add `--root '/path/to/DJ Music'` to these commands too. Existing CSV edits take precedence during a scan. Empty cells do not erase existing tags, even with `--overwrite`. Back up your library before bulk edits. `_sources` and hidden staging folders are excluded from scans. Do not edit the CSV while a download/import is running.

Import files from `tracks` into rekordbox. If they were already imported, use rekordbox's tag-reload command to refresh its cached metadata. File moves can require relocation in rekordbox; this tool never edits its database.

## Checks and troubleshooting

```sh
.venv/bin/python test_import.py
.venv/bin/python test_ui.py
.venv/bin/python music_pipeline.py test

# YouTube changes frequently; update the downloader if extraction breaks
.venv/bin/python -m pip install --upgrade 'yt-dlp[default]'
```

The checks are offline and use generated audio, not copyrighted songs. YouTube may block some networks, videos, or automated requests; private, unavailable, age-restricted, or region-blocked videos may fail. No existing track is replaced on failure. Interrupted downloads are cleaned out of staging; if publishing an import is interrupted, inspect the named `_sources/VIDEO_ID` archive before retrying. If an MP3 exists but CSV writing failed, run `music_pipeline.py scan` to recover the sheet.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org/) and [Mutagen](https://mutagen.readthedocs.io/). Local audio, metadata sheets, browser cookies and the virtual environment are excluded from Git.
