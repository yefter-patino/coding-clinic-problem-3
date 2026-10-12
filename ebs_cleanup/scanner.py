"""Look for unattached EBS volumes in every account and region."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from ebs_cleanup.accounts import list_organization_accounts, select_accounts
from ebs_cleanup.aws import (
    make_client,
    make_session,
    partition_of,
    session_for_account,
    whoami,
)
from ebs_cleanup.models import Account, Finding, ScanError, ScanResult, iso_utc
from ebs_cleanup.pricing import estimate_monthly_cost, load_prices

log = logging.getLogger(__name__)

# The EBS CSI driver adds this tag to volumes it creates for Kubernetes PersistentVolumeClaims.
K8S_PVC_TAG = "kubernetes.io/created-for/pvc/name"


# ---------------------------------------------------------------- protected tags
def parse_protect_tags(items):
    """['keep', 'owner=finance'] -> [('keep', None), ('owner', 'finance')]"""
    rules = []
    for item in items:
        key, has_value, value = item.partition("=")
        rules.append((key.strip(), value.strip() if has_value else None))
    return rules


def protection_reason(tags, rules):
    """Return why a volume is protected (empty text = not protected)."""
    for key, value in rules:
        if key in tags and (value is None or tags[key] == value):
            return f"tag {key}" if value is None else f"tag {key}={value}"
    return ""


# ---------------------------------------------------------------- one account / region
def list_regions(ec2):
    """Regions this account can use (AWS already leaves out regions that are not enabled)."""
    return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])


def find_unattached(ec2, account, region, settings, now):
    """Unattached ('available') volumes that are at least settings.min_age_days old."""
    rules = parse_protect_tags(settings.protect_tags)
    findings = []
    pages = ec2.get_paginator("describe_volumes").paginate(
        Filters=[{"Name": "status", "Values": ["available"]}]
    )
    for page in pages:
        for volume in page["Volumes"]:
            if volume.get("Attachments"):  # belt and braces: 'available' means none
                continue
            age_days = (now - volume["CreateTime"]).days
            if age_days < settings.min_age_days:
                continue
            tags = {t["Key"]: t["Value"] for t in volume.get("Tags", [])}
            reason = protection_reason(tags, rules)
            findings.append(
                Finding(
                    account_id=account.account_id,
                    account_name=account.name,
                    region=region,
                    volume_id=volume["VolumeId"],
                    size_gb=volume["Size"],
                    volume_type=volume["VolumeType"],
                    iops=volume.get("Iops"),
                    throughput_mbps=volume.get("Throughput"),
                    encrypted=volume.get("Encrypted", False),
                    availability_zone=volume["AvailabilityZone"],
                    source_snapshot_id=volume.get("SnapshotId", ""),
                    created=iso_utc(volume["CreateTime"]),
                    age_days=age_days,
                    tags=tags,
                    protected=bool(reason),
                    protect_reason=reason,
                )
            )
    return findings


def make_error(account_id, region, step, err):
    code = err.response["Error"]["Code"] if isinstance(err, ClientError) else type(err).__name__
    return ScanError(account_id, region, step, code, str(err)[:300])


def scan_account(settings, account, identity, now):
    """Scan every region of one account. Returns (findings, errors, regions_scanned)."""
    findings, errors, scanned = [], [], 0

    try:
        # A session per account (and per thread): boto3 sessions are not thread safe.
        base = make_session(settings.profile, settings.home_region)
        if account.account_id == identity["Account"]:
            session = base  # our own account: no role needed
        else:
            session = session_for_account(
                base,
                account.account_id,
                settings.role_name,
                partition_of(identity["Arn"]),
                settings.external_id,
                f"{settings.name_prefix}-ebs-cleanup",
                settings.home_region,
            )
        regions = settings.regions or list_regions(make_client(session, "ec2", settings.home_region))
    except (ClientError, BotoCoreError) as err:
        log.warning("Cannot reach account %s: %s", account.account_id, err)
        return [], [make_error(account.account_id, "-", "connect", err)], 0

    for region in regions:
        try:
            ec2 = make_client(session, "ec2", region)
            found = find_unattached(ec2, account, region, settings, now)
            findings.extend(found)
            scanned += 1
            log.info("%s %s: %d old unattached volume(s)", account.account_id, region, len(found))
        except (ClientError, BotoCoreError) as err:
            log.warning("Scan failed in %s %s: %s", account.account_id, region, err)
            errors.append(make_error(account.account_id, region, "describe_volumes", err))
    return findings, errors, scanned


# ---------------------------------------------------------------- the whole scan
def run_scan(settings, now=None):
    """Discover accounts, scan them in parallel, price the findings."""
    now = now or datetime.now(timezone.utc)
    session = make_session(settings.profile, settings.home_region)
    identity = whoami(session)

    if settings.scope == "organization":
        accounts = list_organization_accounts(session)
        accounts = select_accounts(accounts, settings.include_accounts, settings.exclude_accounts)
    else:
        accounts = [Account(identity["Account"], "current-account")]

    prices = load_prices(settings.prices_file)

    findings, errors, regions_scanned = [], [], 0
    with ThreadPoolExecutor(max_workers=max(1, settings.workers)) as pool:
        jobs = [pool.submit(scan_account, settings, a, identity, now) for a in accounts]
        for job in jobs:
            account_findings, account_errors, scanned = job.result()
            findings.extend(account_findings)
            errors.extend(account_errors)
            regions_scanned += scanned

    for finding in findings:
        finding.monthly_cost, finding.price_note = estimate_monthly_cost(
            prices,
            finding.region,
            finding.volume_type,
            finding.size_gb,
            finding.iops,
            finding.throughput_mbps,
        )

    # Same order every run, so two reports can be compared line by line.
    findings.sort(key=lambda f: (f.account_id, f.region, f.volume_id))
    return ScanResult(
        settings=settings,
        identity_arn=identity["Arn"],
        started=iso_utc(now),
        accounts=accounts,
        findings=findings,
        errors=errors,
        regions_scanned=regions_scanned,
        prices_meta={
            "price_list_date": prices.get("price_list_date", "unknown"),
            "file": settings.prices_file,
        },
    )
