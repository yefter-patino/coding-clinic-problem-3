"""Apply a reviewed deletion plan. It is a DRY RUN unless execute=True.

Safety rules, in order, for every volume in the plan:
  1. Look at the volume again right now. The plan may be hours old.
  2. Skip it if it was attached, changed, or got a protected tag since the scan.
  3. Optionally take a snapshot first and wait until it is finished.
  4. Delete it. AWS itself refuses to delete a volume that is in use.
"""

import logging
from dataclasses import dataclass
from math import ceil

from botocore.exceptions import BotoCoreError, ClientError, WaiterError

from ebs_cleanup.aws import make_client, make_session, partition_of, session_for_account, whoami
from ebs_cleanup.models import iso_utc
from ebs_cleanup.scanner import parse_protect_tags, protection_reason

log = logging.getLogger(__name__)

# Statuses that mean "something went wrong" (the command then exits with code 2).
PROBLEM_STATUSES = {"error", "denied", "snapshot_failed"}


@dataclass
class Result:
    account_id: str
    region: str
    volume_id: str
    status: str
    detail: str = ""
    snapshot_id: str = ""


def get_volume(ec2, volume_id):
    """The volume as AWS describes it now, or None if it no longer exists."""
    try:
        return ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    except ClientError as err:
        if err.response["Error"]["Code"] == "InvalidVolume.NotFound":
            return None
        raise


def reason_to_skip(volume, item, rules):
    """Return (status, detail) when this volume must NOT be deleted, otherwise None."""
    if volume["State"] != "available":
        return "skipped_not_available", f"volume state is now {volume['State']}"
    if volume.get("Attachments"):
        return "skipped_attached", "volume is attached now"
    live_created = iso_utc(volume["CreateTime"])
    if live_created != item["created"]:
        return "skipped_changed", f"created {live_created}, the plan says {item['created']}"
    tags = {t["Key"]: t["Value"] for t in volume.get("Tags", [])}
    reason = protection_reason(tags, rules)
    if reason:
        return "skipped_protected", f"has {reason}"
    return None


def ensure_snapshot(ec2, volume_id, settings):
    """Make sure a finished snapshot of the volume exists and return its id.

    A snapshot made by an earlier (interrupted) run is reused, so running twice is safe.
    """
    creator = f"{settings.name_prefix}-ebs-cleanup"
    found = ec2.describe_snapshots(
        OwnerIds=["self"],
        Filters=[
            {"Name": "tag:SourceVolumeId", "Values": [volume_id]},
            {"Name": "tag:CreatedBy", "Values": [creator]},
        ],
    )["Snapshots"]

    for snap in found:
        if snap["State"] == "completed":
            return snap["SnapshotId"]

    pending = [s for s in found if s["State"] == "pending"]
    if pending:
        snapshot_id = pending[0]["SnapshotId"]
    else:
        snapshot_id = ec2.create_snapshot(
            VolumeId=volume_id,
            Description=f"Safety copy before {creator} deleted {volume_id}",
            TagSpecifications=[
                {
                    "ResourceType": "snapshot",
                    "Tags": [
                        {"Key": "Name", "Value": f"{creator}-{volume_id}"},
                        {"Key": "SourceVolumeId", "Value": volume_id},
                        {"Key": "CreatedBy", "Value": creator},
                    ],
                }
            ],
        )["SnapshotId"]

    attempts = max(1, ceil(settings.snapshot_wait_minutes * 60 / settings.poll_seconds))
    ec2.get_waiter("snapshot_completed").wait(
        SnapshotIds=[snapshot_id],
        WaiterConfig={"Delay": settings.poll_seconds, "MaxAttempts": attempts},
    )
    return snapshot_id


def process_item(session, item, settings, rules, execute, snapshot_first):
    """Handle one volume from the plan and say what happened."""
    result = Result(item["account_id"], item["region"], item["volume_id"], "")
    volume_id = item["volume_id"]
    try:
        ec2 = make_client(session, "ec2", item["region"])

        volume = get_volume(ec2, volume_id)
        if volume is None:
            result.status, result.detail = "already_gone", "volume no longer exists"
            return result

        skip = reason_to_skip(volume, item, rules)
        if skip:
            result.status, result.detail = skip
            return result

        if snapshot_first and execute:
            try:
                result.snapshot_id = ensure_snapshot(ec2, volume_id, settings)
            except (ClientError, BotoCoreError, WaiterError) as err:
                result.status, result.detail = "snapshot_failed", f"not deleted: {err}"[:300]
                return result

        try:
            # DryRun=True asks AWS "would this work?" and deletes nothing.
            ec2.delete_volume(VolumeId=volume_id, DryRun=not execute)
        except ClientError as err:
            code = err.response["Error"]["Code"]
            if code == "DryRunOperation":
                result.status = "would_delete"
                result.detail = "dry run: allowed" + (", would snapshot first" if snapshot_first else "")
            elif code == "InvalidVolume.NotFound":
                result.status, result.detail = "already_gone", "volume no longer exists"
            elif code == "VolumeInUse":
                result.status, result.detail = "skipped_attached", "AWS says the volume is in use"
            elif code in ("UnauthorizedOperation", "AccessDenied"):
                result.status, result.detail = "denied", str(err)[:300]
            else:
                result.status, result.detail = "error", f"{code}: {err}"[:300]
            return result

        result.status = "deleted" if execute else "would_delete"
        return result
    except (ClientError, BotoCoreError) as err:
        result.status, result.detail = "error", str(err)[:300]
        return result


def apply_plan(plan, settings, execute=False, snapshot_first=False):
    """Run the plan. Returns a list of Result objects, one per 'delete' item."""
    items = [i for i in plan["items"] if i["action"] == "delete"]
    rules = parse_protect_tags(plan.get("parameters", {}).get("protect_tags", []))

    base = make_session(settings.profile, settings.home_region)
    identity = whoami(base)
    partition = partition_of(identity["Arn"])

    by_account = {}
    for item in items:
        by_account.setdefault(item["account_id"], []).append(item)

    results = []
    for account_id, account_items in by_account.items():
        try:
            if account_id == identity["Account"]:
                session = base
            elif not settings.role_name:
                raise ValueError("pass --role-name to work in other accounts")
            else:
                session = session_for_account(
                    base,
                    account_id,
                    settings.role_name,
                    partition,
                    settings.external_id,
                    f"{settings.name_prefix}-ebs-cleanup",
                    settings.home_region,
                )
        except (ClientError, BotoCoreError, ValueError) as err:
            log.warning("Cannot work in account %s: %s", account_id, err)
            for item in account_items:
                results.append(
                    Result(
                        account_id,
                        item["region"],
                        item["volume_id"],
                        "error",
                        f"cannot access account: {err}"[:300],
                    )
                )
            continue

        for item in account_items:
            result = process_item(session, item, settings, rules, execute, snapshot_first)
            log.info("%s %s %s -> %s", result.account_id, result.region, result.volume_id, result.status)
            results.append(result)
    return results
