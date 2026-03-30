#!/usr/bin/env python3
"""
Transcribe new voice memo files from a mounted recorder into Simplenote.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "voice_memo_sync.log"
CACHE_FILE = SCRIPT_DIR / ".sync_cache.json"
LOCK_FILE = SCRIPT_DIR / ".sync.lock"
COHERE_TRANSCRIPTION_API_URL = "https://api.cohere.com/v2/audio/transcriptions"
SONIOX_API_BASE_URL = "https://api.soniox.com"
SUPPORTED_TRANSCRIPTION_PROVIDERS = {"cohere", "soniox"}
SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".amr",
    ".asf",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".wav",
    ".webm",
}
DEFAULT_TRANSCRIPTION_PROVIDER = "cohere"
DEFAULT_COHERE_MODEL = "cohere-transcribe-03-2026"
DEFAULT_COHERE_LANGUAGE = "en"
DEFAULT_NOTE_TAG = "voice-memo"
DEFAULT_BOOTSTRAP_THRESHOLD = 20
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 1800.0
DEFAULT_MOUNT_BASENAMES = ("L87",)
DEFAULT_READ_RETRY_COUNT = 3
DEFAULT_READ_RETRY_DELAY_SECONDS = 1.0
COPY_CHUNK_SIZE = 1024 * 1024

logger = logging.getLogger("voice_memo_sync")


class AlreadyRunningError(RuntimeError):
    """Raised when another sync process is already holding the lock."""


@dataclass(frozen=True)
class Recording:
    """Represents a voice memo file on the mounted device."""

    mount_path: Path
    file_path: Path
    relative_path: Path
    recorded_at: datetime
    fingerprint: str

    @property
    def note_title(self) -> str:
        return format_note_title(self.recorded_at)


def configure_logging(verbose: bool) -> None:
    """Configure console and file logging."""
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    logger.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    logger.addHandler(file_handler)


def load_environment() -> None:
    """Load environment variables from .env if available."""
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError(
            "python-dotenv is not installed. Run `uv sync` in this directory first."
        ) from exc

    load_dotenv(env_path)


def load_cache(cache_file: Path) -> dict:
    """Load the sync cache from disk."""
    if not cache_file.exists():
        return {"version": 1, "files": {}}

    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load cache %s: %s", cache_file, exc)
        return {"version": 1, "files": {}}

    if not isinstance(data, dict):
        return {"version": 1, "files": {}}

    files = data.get("files")
    if not isinstance(files, dict):
        files = {}

    return {"version": 1, "files": files}


def save_cache(cache_file: Path, cache: dict) -> None:
    """Persist the sync cache to disk."""
    cache_file.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def parse_csv_env(name: str) -> list[str]:
    """Split a comma-separated env var into trimmed values."""
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def transcription_provider() -> str:
    """Return the configured transcription provider."""
    provider = os.getenv(
        "VOICE_MEMO_TRANSCRIPTION_PROVIDER", DEFAULT_TRANSCRIPTION_PROVIDER
    ).strip().lower()
    if provider not in SUPPORTED_TRANSCRIPTION_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_TRANSCRIPTION_PROVIDERS))
        raise RuntimeError(
            "Unsupported VOICE_MEMO_TRANSCRIPTION_PROVIDER "
            f"{provider!r}. Expected one of: {supported}."
        )
    return provider


def get_device_user() -> str:
    """Return the username used for removable-media mount points."""
    return (
        os.getenv("VOICE_MEMO_USER")
        or os.getenv("USER")
        or os.getenv("USERNAME")
        or "user"
    )


def get_mount_roots() -> list[Path]:
    """Return directories that commonly contain removable-media mounts."""
    user = get_device_user()
    return [
        Path(f"/media/{user}"),
        Path(f"/run/media/{user}"),
        Path("/mnt"),
    ]


def preferred_mount_paths() -> list[Path]:
    """Return preferred recorder mount paths for this device."""
    user = get_device_user()
    return [
        Path(f"/media/{user}/{basename}")
        for basename in DEFAULT_MOUNT_BASENAMES
    ] + [
        Path(f"/run/media/{user}/{basename}")
        for basename in DEFAULT_MOUNT_BASENAMES
    ]


def get_explicit_mount_paths(cli_mount_paths: list[str] | None) -> list[Path]:
    """Return mount paths explicitly configured by CLI or env."""
    raw_paths: list[str] = []
    if cli_mount_paths:
        raw_paths.extend(cli_mount_paths)
    raw_paths.extend(parse_csv_env("VOICE_MEMO_MOUNT_PATHS"))

    unique_paths: list[Path] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        path = Path(raw_path).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path)

    return unique_paths


def candidate_mount_paths(cli_mount_paths: list[str] | None) -> list[Path]:
    """Build the list of mount directories to scan."""
    explicit_paths = get_explicit_mount_paths(cli_mount_paths)
    if explicit_paths:
        return [path for path in explicit_paths if path.exists()]

    candidates: list[Path] = [path for path in preferred_mount_paths() if path.exists()]
    for root in get_mount_roots():
        if root.is_dir():
            child_directories = [child for child in sorted(root.iterdir()) if child.is_dir()]
            if child_directories:
                candidates.extend(child_directories)
            elif root == Path("/mnt"):
                candidates.append(root)

    unique_candidates: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(path)

    return unique_candidates


def list_audio_files_under_mount(mount_path: Path) -> list[Path]:
    """Return supported audio files under a mount path."""
    search_roots: list[Path] = []
    record_dir = mount_path / "RECORD"
    if record_dir.is_dir():
        search_roots.append(record_dir)
    search_roots.append(mount_path)

    files: list[Path] = []
    seen: set[Path] = set()
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for file_path in sorted(search_root.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue
            if file_path in seen:
                continue
            seen.add(file_path)
            files.append(file_path)

    return files


def infer_recorded_at(file_path: Path) -> datetime:
    """Infer the recording timestamp from the filename or file metadata."""
    stem = file_path.stem
    if stem.isdigit() and len(stem) == 14:
        try:
            return datetime.strptime(stem, "%Y%m%d%H%M%S")
        except ValueError:
            pass

    return datetime.fromtimestamp(file_path.stat().st_mtime)


def build_recording_fingerprint(mount_path: Path, file_path: Path) -> str:
    """Build a stable fingerprint for a file without hashing the entire payload."""
    stat = file_path.stat()
    relative_path = file_path.relative_to(mount_path).as_posix()
    payload = f"{relative_path}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def discover_recordings(cli_mount_paths: list[str] | None) -> list[Recording]:
    """Discover mounted recorder files."""
    recordings: list[Recording] = []
    seen_fingerprints: set[str] = set()
    for mount_path in candidate_mount_paths(cli_mount_paths):
        audio_files = list_audio_files_under_mount(mount_path)
        if not audio_files:
            continue

        logger.debug("Found %d audio files under %s", len(audio_files), mount_path)

        for file_path in audio_files:
            relative_path = file_path.relative_to(mount_path)
            fingerprint = build_recording_fingerprint(mount_path, file_path)
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)
            recordings.append(
                Recording(
                    mount_path=mount_path,
                    file_path=file_path,
                    relative_path=relative_path,
                    recorded_at=infer_recorded_at(file_path),
                    fingerprint=fingerprint,
                )
            )

    recordings.sort(
        key=lambda recording: (
            recording.recorded_at,
            recording.relative_path.as_posix(),
        )
    )
    return recordings


def format_note_title(recorded_at: datetime) -> str:
    """Format the Simplenote title requested by the user."""
    return f"{recorded_at.strftime('%Y%m%d_%H%M%S')}_voice_memo"


def build_note_content(recording: Recording, transcript_text: str) -> str:
    """Build note content. Simplenote uses the first line as the note title."""
    safe_transcript = transcript_text.strip() or "[No speech detected by the transcriber.]"
    recorded_at = recording.recorded_at.strftime("%Y-%m-%d %H:%M:%S")
    imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_path = recording.relative_path.as_posix()
    mount_path = recording.mount_path.as_posix()

    return "\n".join(
        [
            recording.note_title,
            "",
            f"Recorded at: {recorded_at}",
            f"Source file: {source_path}",
            f"Mounted at: {mount_path}",
            f"Imported at: {imported_at}",
            f"Fingerprint: {recording.fingerprint}",
            "",
            safe_transcript,
        ]
    )


def ensure_requests():
    """Import requests lazily so unit tests do not require it."""
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError(
            "requests is not installed. Run `uv sync` in this directory first."
        ) from exc

    return requests


def ensure_simplenote():
    """Import simplenote lazily so unit tests do not require it."""
    try:
        import simplenote
    except ImportError as exc:
        raise RuntimeError(
            "simplenote is not installed. Run `uv sync` in this directory first."
        ) from exc

    return simplenote


def new_cohere_session():
    """Create an authenticated Cohere HTTP session."""
    requests = ensure_requests()
    api_key = os.getenv("CO_API_KEY") or os.getenv("COHERE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing CO_API_KEY in .env or the environment. "
            "COHERE_API_KEY is also accepted for compatibility."
        )

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {api_key}"
    session.headers["Accept"] = "application/json"
    return session


def new_soniox_session():
    """Create an authenticated Soniox HTTP session."""
    requests = ensure_requests()
    api_key = os.getenv("SONIOX_API_KEY")
    if not api_key:
        raise RuntimeError("Missing SONIOX_API_KEY in .env or the environment.")

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {api_key}"
    return session


def new_transcription_session(provider: str):
    """Create an authenticated HTTP session for the selected provider."""
    if provider == "cohere":
        return new_cohere_session()
    if provider == "soniox":
        return new_soniox_session()
    raise RuntimeError(f"Unsupported transcription provider: {provider}")


def build_cohere_form_data() -> dict[str, str]:
    """Build the Cohere transcription form payload."""
    form_data = {"model": os.getenv("COHERE_MODEL", DEFAULT_COHERE_MODEL)}
    language = os.getenv("COHERE_LANGUAGE", DEFAULT_COHERE_LANGUAGE).strip()
    if language:
        form_data["language"] = language
    return form_data


def extract_cohere_transcript_text(payload: dict) -> str:
    """Extract the transcript text from a Cohere response payload."""
    for key in ("text", "transcript"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("results", "segments", "utterances"):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        combined = " ".join(
            item.get("text", "").strip()
            for item in value
            if isinstance(item, dict) and item.get("text")
        ).strip()
        if combined:
            return combined

    available_keys = ", ".join(sorted(payload.keys()))
    raise RuntimeError(
        "Cohere transcription response did not include transcript text. "
        f"Response keys: {available_keys or '[none]'}"
    )


def build_soniox_config(file_id: str, client_reference_id: str) -> dict:
    """Build the Soniox transcription request body."""
    config: dict = {
        "model": os.getenv("SONIOX_MODEL", "stt-async-v4"),
        "file_id": file_id,
        "client_reference_id": client_reference_id,
        "enable_language_identification": True,
        "enable_speaker_diarization": False,
    }

    language_hints = parse_csv_env("SONIOX_LANGUAGE_HINTS")
    if language_hints:
        config["language_hints"] = language_hints

    context_text = os.getenv("SONIOX_CONTEXT_TEXT")
    if context_text:
        config["context"] = {"text": context_text}

    return config


def upload_audio_file(session, file_path: Path) -> str:
    """Upload audio to Soniox and return the file ID."""
    with file_path.open("rb") as file_handle:
        response = session.post(
            f"{SONIOX_API_BASE_URL}/v1/files",
            files={"file": (file_path.name, file_handle)},
            timeout=120,
        )

    response.raise_for_status()
    return response.json()["id"]


def stage_recording_for_upload(recording: Recording) -> Path:
    """Copy a recording to local temporary storage before upload."""
    retry_count = int(
        os.getenv("VOICE_MEMO_READ_RETRY_COUNT", str(DEFAULT_READ_RETRY_COUNT))
    )
    retry_delay = float(
        os.getenv(
            "VOICE_MEMO_READ_RETRY_DELAY_SECONDS",
            str(DEFAULT_READ_RETRY_DELAY_SECONDS),
        )
    )

    last_error: Exception | None = None
    for attempt in range(1, retry_count + 1):
        temp_file = tempfile.NamedTemporaryFile(
            prefix="voice_memo_",
            suffix=recording.file_path.suffix,
            delete=False,
        )
        temp_path = Path(temp_file.name)
        temp_file.close()

        try:
            with recording.file_path.open("rb") as source_handle, temp_path.open(
                "wb"
            ) as temp_handle:
                shutil.copyfileobj(source_handle, temp_handle, length=COPY_CHUNK_SIZE)
            return temp_path
        except OSError as exc:
            last_error = exc
            temp_path.unlink(missing_ok=True)
            logger.warning(
                "Failed to read %s on attempt %d/%d: %s",
                recording.file_path,
                attempt,
                retry_count,
                exc,
            )
            if attempt < retry_count:
                time.sleep(retry_delay)

    raise RuntimeError(
        f"Failed to stage recording from device after {retry_count} attempts: {recording.file_path}"
    ) from last_error


def create_transcription(session, config: dict) -> str:
    """Start a Soniox transcription job."""
    response = session.post(
        f"{SONIOX_API_BASE_URL}/v1/transcriptions",
        json=config,
        timeout=120,
    )
    response.raise_for_status()
    return response.json()["id"]


def wait_for_transcription(session, transcription_id: str) -> None:
    """Poll Soniox until the transcription completes or fails."""
    poll_interval = float(
        os.getenv(
            "VOICE_MEMO_POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS)
        )
    )
    timeout_seconds = float(
        os.getenv("VOICE_MEMO_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    )
    deadline = time.monotonic() + timeout_seconds

    while True:
        response = session.get(
            f"{SONIOX_API_BASE_URL}/v1/transcriptions/{transcription_id}",
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        status = payload["status"]

        if status == "completed":
            return
        if status == "error":
            error_message = payload.get("error_message") or "Unknown Soniox error."
            raise RuntimeError(f"Soniox transcription failed: {error_message}")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for Soniox transcription {transcription_id}."
            )

        time.sleep(poll_interval)


def fetch_transcript_text(session, transcription_id: str) -> str:
    """Fetch the final transcript text."""
    response = session.get(
        f"{SONIOX_API_BASE_URL}/v1/transcriptions/{transcription_id}/transcript",
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    text = payload.get("text", "")
    if text:
        return text.strip()

    return render_tokens(payload.get("tokens", []))


def render_tokens(tokens: list[dict]) -> str:
    """Fallback transcript rendering when the text field is missing."""
    return "".join(token.get("text", "") for token in tokens).strip()


def delete_transcription(session, transcription_id: str) -> None:
    """Delete a Soniox transcription if it still exists."""
    response = session.delete(
        f"{SONIOX_API_BASE_URL}/v1/transcriptions/{transcription_id}",
        timeout=120,
    )
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


def delete_uploaded_file(session, file_id: str) -> None:
    """Delete an uploaded Soniox file if it still exists."""
    response = session.delete(f"{SONIOX_API_BASE_URL}/v1/files/{file_id}", timeout=120)
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()


def transcribe_with_cohere(session, recording: Recording) -> str:
    """Upload and transcribe a recording with Cohere."""
    staged_path: Path | None = None

    try:
        staged_path = stage_recording_for_upload(recording)
        with staged_path.open("rb") as file_handle:
            response = session.post(
                COHERE_TRANSCRIPTION_API_URL,
                data=build_cohere_form_data(),
                files={"file": (staged_path.name, file_handle)},
                timeout=120,
            )
        response.raise_for_status()
        return extract_cohere_transcript_text(response.json())
    finally:
        if staged_path:
            staged_path.unlink(missing_ok=True)


def transcribe_with_soniox(session, recording: Recording) -> str:
    """Upload, transcribe, fetch, and clean up a recording."""
    file_id: str | None = None
    transcription_id: str | None = None
    staged_path: Path | None = None

    try:
        staged_path = stage_recording_for_upload(recording)
        file_id = upload_audio_file(session, staged_path)
        transcription_id = create_transcription(
            session, build_soniox_config(file_id, recording.note_title)
        )
        wait_for_transcription(session, transcription_id)
        return fetch_transcript_text(session, transcription_id)
    finally:
        if transcription_id:
            try:
                delete_transcription(session, transcription_id)
            except Exception as exc:
                logger.warning(
                    "Failed to delete Soniox transcription %s: %s",
                    transcription_id,
                    exc,
                )
        if file_id:
            try:
                delete_uploaded_file(session, file_id)
            except Exception as exc:
                logger.warning("Failed to delete Soniox file %s: %s", file_id, exc)
        if staged_path:
            staged_path.unlink(missing_ok=True)


def transcribe_recording(provider: str, session, recording: Recording) -> str:
    """Transcribe a recording with the selected provider."""
    if provider == "cohere":
        return transcribe_with_cohere(session, recording)
    if provider == "soniox":
        return transcribe_with_soniox(session, recording)
    raise RuntimeError(f"Unsupported transcription provider: {provider}")


def new_simplenote_client():
    """Create an authenticated Simplenote client."""
    simplenote = ensure_simplenote()
    user = os.getenv("SIMPLENOTE_USER")
    password = os.getenv("SIMPLENOTE_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "Missing SIMPLENOTE_USER or SIMPLENOTE_PASSWORD in .env or the environment."
        )

    return simplenote.Simplenote(user, password)


def create_simplenote_note(simplenote_client, recording: Recording, transcript_text: str) -> dict:
    """Create a Simplenote note for a single recording."""
    note = {
        "content": build_note_content(recording, transcript_text),
        "tags": [os.getenv("VOICE_MEMO_TAG", DEFAULT_NOTE_TAG)],
    }
    result, status = simplenote_client.add_note(note)
    if status != 0:
        raise RuntimeError(f"Failed to create Simplenote note: {result}")
    return result


def cache_entry(recording: Recording, note_key: str | None, status: str) -> dict:
    """Build the cache payload for a processed or skipped file."""
    return {
        "status": status,
        "mount_path": recording.mount_path.as_posix(),
        "relative_path": recording.relative_path.as_posix(),
        "note_key": note_key,
        "note_title": recording.note_title,
        "recorded_at": recording.recorded_at.isoformat(),
        "processed_at": datetime.now().isoformat(),
    }


def mark_recordings_as_seen(
    cache_file: Path, cache: dict, recordings: Iterable[Recording], status: str
) -> int:
    """Store recordings in the cache without transcribing them."""
    count = 0
    for recording in recordings:
        if recording.fingerprint in cache["files"]:
            continue
        cache["files"][recording.fingerprint] = cache_entry(
            recording, note_key=None, status=status
        )
        count += 1

    if count:
        save_cache(cache_file, cache)
    return count


def limit_recordings(recordings: list[Recording], limit: int | None) -> list[Recording]:
    """Apply the optional --limit CLI flag."""
    if limit is None or limit < 0:
        return recordings
    return recordings[:limit]


def bootstrap_threshold() -> int:
    """Return the number of unseen recordings that triggers first-run bootstrapping."""
    raw_value = os.getenv(
        "VOICE_MEMO_BOOTSTRAP_THRESHOLD", str(DEFAULT_BOOTSTRAP_THRESHOLD)
    )
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_BOOTSTRAP_THRESHOLD
    return max(0, value)


@contextmanager
def single_instance_lock(lock_file: Path):
    """Prevent overlapping sync runs."""
    lock_handle = lock_file.open("w", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError("Another sync is already running.") from exc
        yield
    finally:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        finally:
            lock_handle.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mount-path",
        action="append",
        help="Explicit mounted device path to scan. Repeat this flag as needed.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Transcribe all unseen files, including the existing backlog on first run.",
    )
    parser.add_argument(
        "--mark-existing",
        action="store_true",
        help="Record current files in the cache without transcribing them.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be transcribed without calling the transcriber or Simplenote.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only process the first N unseen recordings.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def sync_recordings(args: argparse.Namespace) -> int:
    """Run one sync pass."""
    cache = load_cache(CACHE_FILE)
    recordings = discover_recordings(args.mount_path)
    provider = transcription_provider()

    if not recordings:
        logger.error("Recorder not found or no supported audio files were mounted.")
        logger.info(
            "Scanned mount roots: %s",
            ", ".join(path.as_posix() for path in get_mount_roots()),
        )
        return 1

    unseen_recordings = [
        recording
        for recording in recordings
        if recording.fingerprint not in cache["files"]
    ]
    unseen_recordings = limit_recordings(unseen_recordings, args.limit)

    logger.info("Found %d mounted recording files.", len(recordings))
    logger.info("Found %d unseen recording files.", len(unseen_recordings))
    logger.info("Using transcription provider: %s", provider)

    if not unseen_recordings:
        logger.info("Nothing to transcribe.")
        return 0

    if args.dry_run:
        if args.mark_existing:
            for recording in unseen_recordings:
                logger.info("Would mark as existing %s", recording.file_path)
            return 0

        if not cache["files"] and not args.backfill:
            threshold = bootstrap_threshold()
            if len(unseen_recordings) > threshold:
                logger.info(
                    "Would bootstrap the cache instead of transcribing because %d unseen files exceed the threshold of %d.",
                    len(unseen_recordings),
                    threshold,
                )
                for recording in unseen_recordings:
                    logger.info("Would mark as existing %s", recording.file_path)
                return 0

        for recording in unseen_recordings:
            logger.info(
                "Would transcribe %s -> %s",
                recording.file_path,
                recording.note_title,
            )
        return 0

    if args.mark_existing:
        marked = mark_recordings_as_seen(
            CACHE_FILE, cache, unseen_recordings, status="skipped_existing"
        )
        logger.info("Marked %d recordings as seen without transcribing them.", marked)
        return 0

    if not cache["files"] and not args.backfill:
        threshold = bootstrap_threshold()
        if len(unseen_recordings) > threshold:
            marked = mark_recordings_as_seen(
                CACHE_FILE, cache, unseen_recordings, status="skipped_bootstrap"
            )
            logger.info(
                "Cache was empty and %d unseen files exceeded the bootstrap threshold of %d.",
                len(unseen_recordings),
                threshold,
            )
            logger.info(
                "Marked %d existing files as seen. Use --backfill if you want the backlog transcribed.",
                marked,
            )
            return 0

    transcription_session = new_transcription_session(provider)
    simplenote_client = new_simplenote_client()

    created_notes = 0
    failed_notes = 0
    for recording in unseen_recordings:
        logger.info("Transcribing %s", recording.file_path)
        try:
            transcript_text = transcribe_recording(
                provider, transcription_session, recording
            )
            note = create_simplenote_note(simplenote_client, recording, transcript_text)
            cache["files"][recording.fingerprint] = cache_entry(
                recording,
                note_key=note.get("key"),
                status="transcribed",
            )
            save_cache(CACHE_FILE, cache)
            created_notes += 1
            logger.info("Created Simplenote note %s", recording.note_title)
        except Exception as exc:
            failed_notes += 1
            logger.error("Failed to process %s: %s", recording.file_path, exc)
            if not recording.mount_path.exists():
                logger.error(
                    "Recorder mount %s disappeared during sync. Reconnect the device and rerun the command to resume from the cache.",
                    recording.mount_path,
                )
                break

    logger.info("Created %d new Simplenote notes.", created_notes)
    if failed_notes:
        logger.warning("%d recordings failed and were left unprocessed.", failed_notes)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv or sys.argv[1:])
    configure_logging(args.verbose)

    logger.info("=" * 50)
    logger.info("Voice Memo Sync")
    logger.info("=" * 50)

    try:
        load_environment()
        with single_instance_lock(LOCK_FILE):
            return sync_recordings(args)
    except AlreadyRunningError as exc:
        logger.info("%s", exc)
        return 0
    except Exception as exc:
        logger.exception("Sync failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
