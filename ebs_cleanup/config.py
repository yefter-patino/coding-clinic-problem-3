"""Settings objects. Every value comes from the command line or an environment variable."""

from dataclasses import dataclass, field


@dataclass
class ScanSettings:
    min_age_days: int
    scope: str = "current"  # "current" = this account only, "organization" = every member account
    role_name: str = ""  # role to assume in each member account (organization scope)
    external_id: str = ""  # optional ExternalId for that role
    profile: str = ""  # optional AWS CLI profile name
    home_region: str = ""  # region used for STS / Organizations / listing regions
    regions: list = field(default_factory=list)  # empty = every region enabled in the account
    include_accounts: list = field(default_factory=list)  # empty = all accounts
    exclude_accounts: list = field(default_factory=list)
    protect_tags: list = field(default_factory=list)  # "key" or "key=value"
    report_tags: list = field(default_factory=lambda: ["Name"])  # tag keys shown as columns
    prices_file: str = "config/prices.json"
    workers: int = 4  # how many accounts are scanned at the same time
    name_prefix: str = "yefter"  # used to name the session and any snapshot we create


@dataclass
class ApplySettings:
    role_name: str = ""  # role with delete permissions in each member account
    external_id: str = ""
    profile: str = ""
    home_region: str = ""
    name_prefix: str = "yefter"
    snapshot_wait_minutes: int = 60  # how long to wait for a safety snapshot to finish
    poll_seconds: int = 15  # how often to check the snapshot
