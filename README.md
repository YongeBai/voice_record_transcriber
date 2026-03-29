# Actions Ami Voice Memo Sync

Automatically transcribe new recordings from an Actions Ami / AK1025 MP4 player into Simplenote whenever the device is plugged in.

The recorder currently mounts on this machine with a `RECORD/` directory under `/media/yongebai/L87`, and the script now explicitly prefers that mount path before falling back to generic removable-media discovery.

## What It Does

- Detects mounted recorder volumes under `/media/$USER`, `/run/media/$USER`, or `/mnt`
- Finds supported audio files such as `.wav`, `.mp3`, `.m4a`, and `.mp4`
- Uses the Soniox async transcription API to transcribe each unseen file
- Creates one Simplenote note per recording
- Uses note titles in the format `YYYYMMDD_HHMMSS_voice_memo`
- Keeps a local cache so each file is only processed once
- Skips the existing backlog on the first run if the device already contains many old files

## Setup

1. Install the project:

   ```bash
   cd ~/voice_record_transcriber
   ./install.sh
   ```

2. Create and edit `.env`:

   ```bash
   cp env.example .env
   nano .env
   ```

3. Set the required secrets:

   ```env
   SIMPLENOTE_USER=your-email@example.com
   SIMPLENOTE_PASSWORD=your-password
   SONIOX_API_KEY=your-soniox-api-key
   ```

4. Optional but recommended for this recorder: seed the cache with the existing files so only future memos are transcribed:

   ```bash
   uv run voice_memo_sync.py --mark-existing
   ```

## Manual Usage

Plug in the recorder, wait for it to mount, then run:

```bash
uv run voice_memo_sync.py
```

Useful flags:

- `--dry-run`: show what would be processed without calling Soniox or Simplenote
- `--mark-existing`: add the currently mounted files to the cache without transcribing them
- `--backfill`: transcribe all unseen files even on the first run
- `--mount-path /path/to/mount`: explicitly scan a mount path
- `--limit N`: only process the first `N` unseen files

If you want to force this exact mount in your `.env`, set:

```env
VOICE_MEMO_MOUNT_PATHS=/media/yongebai/L87
```

## Automatic Sync On Plug-In

1. Copy the `udev` rule:

   ```bash
   sudo cp 99-actions-ami-voice-memo.rules /etc/udev/rules.d/
   ```

2. Copy the systemd service:

   ```bash
   sudo cp actions-ami-voice-memo@.service /etc/systemd/system/
   ```

3. Reload `udev` and systemd:

   ```bash
   sudo udevadm control --reload-rules
   sudo systemctl daemon-reload
   sudo systemctl enable actions-ami-voice-memo@$(whoami).service
   ```

The included rule is already wired to the detected USB IDs for this device:

- Vendor ID: `10d6`
- Product ID: `1101`

If your Linux username is not `yongebai`, edit `99-actions-ami-voice-memo.rules` before copying it into `/etc/udev/rules.d/`.

## Environment Variables

Required:

- `SIMPLENOTE_USER`
- `SIMPLENOTE_PASSWORD`
- `SONIOX_API_KEY`

Optional:

- `SONIOX_MODEL`: defaults to `stt-async-v4`
- `SONIOX_LANGUAGE_HINTS`: comma-separated language hints like `en,es`
- `SONIOX_CONTEXT_TEXT`: extra Soniox context
- `VOICE_MEMO_USER`: overrides `$USER` for removable-media mount detection
- `VOICE_MEMO_MOUNT_PATHS`: comma-separated explicit mount paths
- `VOICE_MEMO_TAG`: Simplenote tag, default `voice-memo`
- `VOICE_MEMO_BOOTSTRAP_THRESHOLD`: first-run backlog threshold, default `20`
- `VOICE_MEMO_POLL_INTERVAL_SECONDS`: Soniox polling interval, default `2`
- `VOICE_MEMO_TIMEOUT_SECONDS`: transcription timeout, default `1800`

## First-Run Behavior

This recorder already contains a large backlog of old recordings. To avoid sending all of them to Soniox on the first automatic sync, the script does this when the cache is empty:

- If the number of unseen recordings is greater than `VOICE_MEMO_BOOTSTRAP_THRESHOLD`, it treats them as existing files and stores them in the cache without transcribing them.
- If you want the full backlog transcribed anyway, run `uv run voice_memo_sync.py --backfill`.

If you prefer explicit control, run `uv run voice_memo_sync.py --mark-existing` once before enabling the `udev` rule.

## Simplenote Note Format

Each note starts with the title line that Simplenote uses as the note title:

```text
20260328_193000_voice_memo

Recorded at: 2026-03-28 19:30:00
Source file: RECORD/20260328193000.WAV
Mounted at: /media/yongebai/L87
Imported at: 2026-03-28 19:34:12
Fingerprint: ...

Transcribed text goes here.
```
