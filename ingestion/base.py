"""
Base source abstraction for the Source Sync Engine.

Every external data source (WhatsApp, DLD, Bayut, etc.) implements:

    discover_jobs()  → list[SyncJob]
    fetch_records(job) → Iterator[SourceRecord]

Records flow through the pipeline: store_raw → parse → resolve → observe.
"""

from dataclasses import dataclass, field
from typing import Iterator, Optional
from datetime import datetime, timezone


# ── Data types ────────────────────────────────────────────────────

@dataclass
class SyncJob:
    """
    A sync job represents one unit of work for a source.
    For WhatsApp: one group = one job.
    For DLD: one area/time-window = one job.
    """
    source: str                # "whatsapp", "dld", "bayut", etc.
    instance: str              # e.g., "propai-whatsapp" for WhatsApp instance
    group_id: str              # e.g., WhatsApp JID, DLD dataset key
    group_name: str = ""       # human-readable label
    meta: dict = field(default_factory=dict)  # source-specific metadata

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "instance": self.instance,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "meta": self.meta,
        }


@dataclass
class SourceRecord:
    """
    One raw record from a source, before any parsing.

    Fields are generic enough for any source.
    """
    source: str                # "whatsapp", "igr", etc.
    instance: str              # source instance identifier
    group_id: str              # source group / dataset
    record_id: str             # unique ID within the source
    text: str                  # main textual content
    sender: str = ""           # originator
    timestamp: Optional[float] = None  # UNIX timestamp
    raw: dict = field(default_factory=dict)  # full source payload
    meta: dict = field(default_factory=dict)  # additional metadata

    @property
    def message_uid(self) -> str:
        """Globally unique identifier for dedup."""
        return f"{self.source}::{self.instance}::{self.group_id}::{self.record_id}"

    @property
    def timestamp_iso(self) -> str:
        if self.timestamp and self.timestamp > 1000000000:
            return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "instance": self.instance,
            "group_id": self.group_id,
            "record_id": self.record_id,
            "text": self.text,
            "sender": self.sender,
            "timestamp": self.timestamp,
            "timestamp_iso": self.timestamp_iso,
            "message_uid": self.message_uid,
            "raw": self.raw,
            "meta": self.meta,
        }


# ── Base Source ───────────────────────────────────────────────────

class BaseSource:
    """Abstract base for all data sources."""

    # Unique source identifier (used in DB and API routes)
    name: str = "unknown"

    # Source version — bump when parser/resolver logic changes for this source
    version: str = "1.0.0"

    def discover_jobs(self) -> list[SyncJob]:
        """
        Discover all available sync jobs for this source.

        For WhatsApp: enumerate all joined groups.
        For IGR: list available datasets / years.
        """
        raise NotImplementedError

    def fetch_records(self, job: SyncJob) -> Iterator[SourceRecord]:
        """
        Yield SourceRecords for a given job.

        Called by the scheduler. Records should be yielded oldest-first
        to maintain chronological order.
        """
        raise NotImplementedError

    def validate_connection(self) -> bool:
        """Check if the source is reachable / authenticated."""
        return True

    def __repr__(self) -> str:
        return f"<Source {self.name} v{self.version}>"
