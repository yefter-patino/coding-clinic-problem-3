"""Small data containers shared by the other modules."""

from dataclasses import dataclass, field
from datetime import timezone


def iso_utc(moment):
    """Turn a datetime into text like 2026-10-12T15:30:00Z (always UTC, no fractions)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Account:
    account_id: str
    name: str


@dataclass
class Finding:
    """One unattached volume that is old enough to be reported."""

    account_id: str
    account_name: str
    region: str
    volume_id: str
    size_gb: int
    volume_type: str
    iops: int | None
    throughput_mbps: int | None
    encrypted: bool
    availability_zone: str
    source_snapshot_id: str
    created: str  # ISO 8601 UTC text, see iso_utc()
    age_days: int
    tags: dict = field(default_factory=dict)
    monthly_cost: float = 0.0
    price_note: str = ""
    protected: bool = False
    protect_reason: str = ""


@dataclass
class ScanError:
    """Something that failed while scanning (so it is never silently missing from the report)."""

    account_id: str
    region: str
    step: str
    code: str
    message: str


@dataclass
class ScanResult:
    """Everything a scan produced. The report and plan writers read from this."""

    settings: object
    identity_arn: str
    started: str
    accounts: list
    findings: list
    errors: list
    regions_scanned: int
    prices_meta: dict = field(default_factory=dict)
