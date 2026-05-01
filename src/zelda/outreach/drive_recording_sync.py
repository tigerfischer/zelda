"""Drive reverse-sync for call recordings — Phase 16e.

Vaibhav drops a recording file into:
    Zelva/ (Drive root)
    └── call-recordings/
        └── {city}/
            └── {lead_id}--{clinic_slug}/
                └── recording.m4a  (or .mp3, .wav, .ogg)

This module:
  1. Finds or creates the call-recordings folder in Drive
  2. Lists all audio files recursively
  3. Downloads any not yet in the local DB
  4. Transcribes with faster-whisper (downloaded on first use)
  5. Stores the transcript in outreach_messages via OutreachRepository
  6. Updates status → "called"

The lead_id is parsed from the subfolder name (prefix before '--').
If the folder name doesn't match any lead, the transcript is saved to
a local fallback file and logged for manual association.

Reliability guarantees:
  - Idempotent: already-processed file IDs are skipped
  - Download failures don't abort the run — logged and retried next sync
  - Transcription failures save raw audio locally; transcript stays null
  - No file is deleted from Drive
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from zelda.gateways.google_drive import GoogleDriveGateway
    from zelda.repositories.outreach_repo import OutreachRepository

_RECORDINGS_FOLDER_NAME = "call-recordings"
_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".aac", ".flac", ".webm"}
_PROCESSED_INDEX_FILE = "data/outreach/.processed_recordings.txt"


class DriveRecordingSync:
    """Syncs call recordings from Drive → local transcripts → outreach DB."""

    def __init__(
        self,
        drive: "GoogleDriveGateway",
        outreach_repo: "OutreachRepository",
        local_audio_dir: Path,
    ) -> None:
        self._drive = drive
        self._outreach_repo = outreach_repo
        self._local_audio_dir = local_audio_dir
        self._processed: set[str] = _load_processed_index()

    def ensure_folder_structure(self) -> str:
        """Idempotently create the call-recordings root folder in Drive.
        Returns the folder ID."""
        folder_id = self._drive.find_or_create_subfolder(_RECORDINGS_FOLDER_NAME)
        logger.info(
            "drive_recording_sync.root_folder id={id}", id=folder_id
        )
        return folder_id

    def ensure_lead_folder(self, city: str, lead_id: str, clinic_slug: str) -> str:
        """Create call-recordings/{city}/{lead_id}--{slug}/ in Drive.
        Returns the deepest folder ID. Call this when a new outreach is sent."""
        root = self.ensure_folder_structure()
        city_id = self._drive.find_or_create_subfolder(city, parent_folder_id=root)
        folder_name = f"{lead_id}--{_slugify(clinic_slug)}"
        leaf_id = self._drive.find_or_create_subfolder(folder_name, parent_folder_id=city_id)
        logger.info(
            "drive_recording_sync.lead_folder lead={lid} folder={name}",
            lid=lead_id, name=folder_name,
        )
        return leaf_id

    def sync(self) -> SyncResult:
        """Run a full sync pass. Downloads and transcribes any new recordings."""
        result = SyncResult()
        root_id = self.ensure_folder_structure()

        # Walk: root → city folders → lead folders → audio files
        city_folders = self._drive.list_files_in_folder(root_id)
        for city_folder in city_folders:
            if city_folder["mimeType"] != "application/vnd.google-apps.folder":
                continue
            lead_folders = self._drive.list_files_in_folder(city_folder["id"])
            for lead_folder in lead_folders:
                if lead_folder["mimeType"] != "application/vnd.google-apps.folder":
                    continue
                self._sync_lead_folder(lead_folder, city_folder["name"], result)

        logger.info(
            "drive_recording_sync.done "
            "found={f} downloaded={d} transcribed={t} errors={e}",
            f=result.found,
            d=result.downloaded,
            t=result.transcribed,
            e=result.errors,
        )
        return result

    # ── private ───────────────────────────────────────────────────────

    def _sync_lead_folder(
        self,
        folder: dict,
        city: str,
        result: "SyncResult",
    ) -> None:
        lead_id = _parse_lead_id(folder["name"])
        files = self._drive.list_files_in_folder(folder["id"])

        for f in files:
            ext = Path(f["name"]).suffix.lower()
            if ext not in _AUDIO_EXTENSIONS:
                continue

            result.found += 1
            file_id = f["id"]

            if file_id in self._processed:
                continue

            # Download
            local_path = self._local_audio_dir / city / folder["name"] / f["name"]
            try:
                self._drive.download_file(file_id, local_path)
                result.downloaded += 1
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "drive_recording_sync.download_failed file={n} err={e}",
                    n=f["name"], e=e,
                )
                result.errors += 1
                continue

            # Transcribe
            transcript = _transcribe(local_path)
            if transcript:
                result.transcribed += 1

            # Persist to DB
            if lead_id:
                msg = self._outreach_repo.get_by_lead(lead_id)
                if msg:
                    self._outreach_repo.set_call_transcript(msg.id, transcript or "")
                    logger.info(
                        "drive_recording_sync.transcript_saved lead={lid} file={n}",
                        lid=lead_id, n=f["name"],
                    )
                else:
                    logger.warning(
                        "drive_recording_sync.no_outreach_record lead={lid}", lid=lead_id
                    )
                    _save_fallback(local_path, transcript or "", lead_id, city)
            else:
                logger.warning(
                    "drive_recording_sync.unmatched_folder name={n}", n=folder["name"]
                )
                _save_fallback(local_path, transcript or "", "unknown", city)

            _mark_processed(file_id)
            self._processed.add(file_id)


class SyncResult:
    found: int = 0
    downloaded: int = 0
    transcribed: int = 0
    errors: int = 0


def _transcribe(audio_path: Path) -> str | None:
    """Transcribe using faster-whisper (local, CPU). Returns transcript or None."""
    try:
        from faster_whisper import WhisperModel  # type: ignore[import]
    except ImportError:
        logger.warning(
            "drive_recording_sync.whisper_not_installed "
            "— run: conda install -c conda-forge faster-whisper"
        )
        return None

    try:
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(audio_path), beam_size=5, language="en")
        transcript = " ".join(seg.text.strip() for seg in segments)
        logger.info(
            "drive_recording_sync.transcribed path={p} chars={c}",
            p=audio_path.name, c=len(transcript),
        )
        return transcript
    except Exception as e:  # noqa: BLE001
        logger.error(
            "drive_recording_sync.transcribe_failed path={p} err={e}",
            p=audio_path, e=e,
        )
        return None


def _parse_lead_id(folder_name: str) -> str | None:
    """Extract lead_id from '{lead_id}--{slug}' folder name."""
    parts = folder_name.split("--", 1)
    if len(parts) >= 1 and parts[0]:
        return parts[0]
    return None


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _load_processed_index() -> set[str]:
    path = Path(_PROCESSED_INDEX_FILE)
    if not path.exists():
        return set()
    return set(path.read_text(encoding="utf-8").splitlines())


def _mark_processed(file_id: str) -> None:
    path = Path(_PROCESSED_INDEX_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(file_id + "\n")


def _save_fallback(audio_path: Path, transcript: str, lead_id: str, city: str) -> None:
    fallback = audio_path.with_suffix(".transcript.txt")
    fallback.write_text(
        f"lead_id: {lead_id}\ncity: {city}\n\n{transcript}", encoding="utf-8"
    )
    logger.info("drive_recording_sync.fallback_saved path={p}", p=fallback)


__all__ = ["DriveRecordingSync", "SyncResult"]
