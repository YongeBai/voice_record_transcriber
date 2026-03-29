import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from voice_memo_sync import (
    build_recording_fingerprint,
    discover_recordings,
    format_note_title,
    infer_recorded_at,
)


class VoiceMemoSyncTests(unittest.TestCase):
    def test_infer_recorded_at_from_filename(self):
        file_path = Path("/tmp/20260328191530.WAV")
        self.assertEqual(infer_recorded_at(file_path), datetime(2026, 3, 28, 19, 15, 30))

    def test_format_note_title(self):
        recorded_at = datetime(2026, 3, 28, 19, 15, 30)
        self.assertEqual(format_note_title(recorded_at), "20260328_191530_voice_memo")

    def test_build_recording_fingerprint_uses_relative_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mount_one = Path(temp_dir) / "L87"
            mount_two = Path(temp_dir) / "ALT"
            record_one = mount_one / "RECORD"
            record_two = mount_two / "RECORD"
            record_one.mkdir(parents=True)
            record_two.mkdir(parents=True)

            file_one = record_one / "20260328191530.WAV"
            file_two = record_two / "20260328191530.WAV"
            file_one.write_bytes(b"abc")
            file_two.write_bytes(b"abc")

            fingerprint_one = build_recording_fingerprint(mount_one, file_one)
            fingerprint_two = build_recording_fingerprint(mount_two, file_two)
            self.assertEqual(fingerprint_one, fingerprint_two)

    def test_discover_recordings_finds_record_folder_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mount_path = Path(temp_dir) / "L87"
            record_dir = mount_path / "RECORD"
            record_dir.mkdir(parents=True)
            audio_file = record_dir / "20260328191530.WAV"
            audio_file.write_bytes(b"audio")

            recordings = discover_recordings([str(mount_path)])

            self.assertEqual(len(recordings), 1)
            self.assertEqual(
                recordings[0].relative_path.as_posix(), "RECORD/20260328191530.WAV"
            )
            self.assertEqual(recordings[0].note_title, "20260328_191530_voice_memo")


if __name__ == "__main__":
    unittest.main()
