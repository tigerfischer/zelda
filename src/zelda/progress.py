"""Live progress tracking for long-running CLI jobs.

Writes a JSON status file after every item so any external reader
(`cat`, `watch cat`, Drive sync) always sees the current state.
Also prints a one-line update to stdout after each item.

Usage:

    tracker = ProgressTracker(
        job="fetch-reviews",
        city="Ludhiana",
        run_id=run_id,
        status_path=data_dir / "progress" / "fetch-reviews-ludhiana.json",
    )
    tracker.set_total(138)          # call once total is known
    tracker.update(name="Clinic X", status="ok", reviews=47)
    tracker.finish()
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


class ProgressTracker:
    """Tracks one long-running job. Thread-unsafe; call from a single thread."""

    def __init__(
        self,
        *,
        job: str,
        city: str,
        run_id: str,
        status_path: Path,
    ) -> None:
        self._job = job
        self._city = city
        self._run_id = run_id
        self._status_path = status_path

        self._total: int = 0
        self._processed: int = 0
        self._successful: int = 0
        self._errored: int = 0
        self._blocked: int = 0
        self._reviews_captured: int = 0
        self._current: str = ""

        self._started_mono = time.monotonic()
        self._started_wall = datetime.now(timezone.utc)

        status_path.parent.mkdir(parents=True, exist_ok=True)
        self._flush("starting")
        self._print_header()

    def set_total(self, total: int) -> None:
        self._total = total
        self._flush("running")

    def update(self, *, name: str, status: str, reviews: int) -> None:
        self._processed += 1
        self._current = name
        self._reviews_captured += reviews

        if status in ("ok", "partial"):
            self._successful += 1
        elif status in ("captcha", "blocked"):
            self._blocked += 1
        else:
            self._errored += 1

        elapsed = time.monotonic() - self._started_mono
        eta_s = self._eta(elapsed)
        self._flush("running", elapsed=elapsed, eta_s=eta_s)
        self._print_line(name=name, status=status, reviews=reviews, elapsed=elapsed, eta_s=eta_s)

    def finish(self, *, blocked: bool = False) -> None:
        elapsed = time.monotonic() - self._started_mono
        final_status = "blocked" if blocked else "done"
        self._flush(final_status, elapsed=elapsed)
        print(
            f"\n{'blocked' if blocked else 'done'}  "
            f"{self._processed}/{self._total} processed  "
            f"{self._reviews_captured} reviews captured  "
            f"{_fmt_dur(elapsed)} elapsed"
        )

    # ── private ────────────────────────────────────────────────────────

    def _eta(self, elapsed: float) -> float | None:
        if self._processed == 0:
            return None
        rate = self._processed / elapsed          # items/s
        remaining = self._total - self._processed
        return remaining / rate if rate > 0 else None

    def _flush(
        self,
        status: str,
        *,
        elapsed: float = 0.0,
        eta_s: float | None = None,
    ) -> None:
        rate_per_hour = (
            round(self._processed / elapsed * 3600, 1) if elapsed > 0 else 0.0
        )
        payload = {
            "job": self._job,
            "status": status,
            "city": self._city,
            "run_id": self._run_id,
            "started_at": self._started_wall.isoformat(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "total": self._total,
            "processed": self._processed,
            "successful": self._successful,
            "errored": self._errored,
            "blocked": self._blocked,
            "reviews_captured": self._reviews_captured,
            "elapsed_s": round(elapsed),
            "rate_per_hour": rate_per_hour,
            "eta_s": round(eta_s) if eta_s is not None else None,
            "eta_human": _fmt_dur(eta_s) if eta_s is not None else None,
            "current": self._current,
        }
        self._status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _print_header(self) -> None:
        print(
            f"fetch-reviews  city={self._city}  run_id={self._run_id}\n"
            f"status file: {self._status_path}\n"
        )

    def _print_line(
        self,
        *,
        name: str,
        status: str,
        reviews: int,
        elapsed: float,
        eta_s: float | None,
    ) -> None:
        pct = self._processed / self._total * 100 if self._total else 0.0
        eta_str = f"~{_fmt_dur(eta_s)} left" if eta_s is not None else "ETA unknown"
        mark = {"ok": "ok", "partial": "partial", "error": "ERR", "captcha": "CAPTCHA", "blocked": "BLOCKED"}.get(status, "?")
        print(
            f"[{self._processed:3d}/{self._total}] {pct:4.0f}%  "
            f"{name[:48]:<48}  {mark:<7}  "
            f"{reviews:4d} rev  "
            f"{_fmt_dur(elapsed)} elapsed  {eta_str}",
            flush=True,
        )


def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m:02d}m"


__all__ = ["ProgressTracker"]
