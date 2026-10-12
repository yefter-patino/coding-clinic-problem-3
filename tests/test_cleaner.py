import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from ebs_cleanup.cleaner import apply_plan
from ebs_cleanup.config import ApplySettings
from ebs_cleanup.plan import build_plan
from ebs_cleanup.scanner import run_scan
from tests.helpers import attach_to_new_instance, create_volume, days_from_now, make_settings

SETTINGS = ApplySettings(home_region="us-east-1", poll_seconds=1, snapshot_wait_minutes=1)


def make_plan(**scan_settings):
    """Scan the (mock) account and turn the findings into a plan."""
    result = run_scan(make_settings(**scan_settings), now=days_from_now(40))
    return build_plan(result)


def volume_exists(ec2, volume_id):
    try:
        ec2.describe_volumes(VolumeIds=[volume_id])
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] == "InvalidVolume.NotFound":
            return False
        raise


def tool_snapshots(ec2, volume_id):
    """Snapshots made for this volume. (Moto also holds many built-in snapshots, so we filter.)"""
    return ec2.describe_snapshots(Filters=[{"Name": "tag:SourceVolumeId", "Values": [volume_id]}])[
        "Snapshots"
    ]


def statuses(results):
    return {r.volume_id: r.status for r in results}


@mock_aws
def test_dry_run_changes_nothing():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()

    results = apply_plan(plan, SETTINGS, execute=False)

    assert statuses(results) == {volume_id: "would_delete"}
    assert volume_exists(ec2, volume_id)


@mock_aws
def test_execute_deletes_the_volume():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()

    results = apply_plan(plan, SETTINGS, execute=True)

    assert statuses(results) == {volume_id: "deleted"}
    assert not volume_exists(ec2, volume_id)


@mock_aws
def test_running_the_same_plan_twice_is_safe():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()

    apply_plan(plan, SETTINGS, execute=True)
    second = apply_plan(plan, SETTINGS, execute=True)

    assert statuses(second) == {volume_id: "already_gone"}


@mock_aws
def test_items_marked_skip_are_never_touched():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()
    plan["items"][0]["action"] = "skip"

    results = apply_plan(plan, SETTINGS, execute=True)

    assert results == []
    assert volume_exists(ec2, volume_id)


@mock_aws
def test_volume_attached_after_the_scan_is_not_deleted():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()
    attach_to_new_instance(ec2, volume_id)

    results = apply_plan(plan, SETTINGS, execute=True)

    assert statuses(results)[volume_id].startswith("skipped_")
    assert volume_exists(ec2, volume_id)


@mock_aws
def test_protect_tag_added_after_the_scan_stops_the_delete():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan(protect_tags=["keep"])
    ec2.create_tags(Resources=[volume_id], Tags=[{"Key": "keep", "Value": "true"}])

    results = apply_plan(plan, SETTINGS, execute=True)

    assert statuses(results) == {volume_id: "skipped_protected"}
    assert volume_exists(ec2, volume_id)


@mock_aws
def test_volume_that_does_not_match_the_plan_is_not_deleted():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()
    plan["items"][0]["created"] = "2020-01-01T00:00:00Z"

    results = apply_plan(plan, SETTINGS, execute=True)

    assert statuses(results) == {volume_id: "skipped_changed"}
    assert volume_exists(ec2, volume_id)


@mock_aws
def test_snapshot_is_taken_before_delete():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()

    results = apply_plan(plan, SETTINGS, execute=True, snapshot_first=True)

    assert statuses(results) == {volume_id: "deleted"}
    snapshots = tool_snapshots(ec2, volume_id)
    assert [s["SnapshotId"] for s in snapshots] == [results[0].snapshot_id]
    tags = {t["Key"]: t["Value"] for t in snapshots[0]["Tags"]}
    assert tags["Name"] == f"yefter-ebs-cleanup-{volume_id}"
    assert tags["SourceVolumeId"] == volume_id
    assert tags["CreatedBy"] == "yefter-ebs-cleanup"


@mock_aws
def test_existing_safety_snapshot_is_reused():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()
    first = apply_plan(plan, SETTINGS, execute=False)  # dry run first, as a user would
    existing = ec2.create_snapshot(
        VolumeId=volume_id,
        TagSpecifications=[
            {
                "ResourceType": "snapshot",
                "Tags": [
                    {"Key": "SourceVolumeId", "Value": volume_id},
                    {"Key": "CreatedBy", "Value": "yefter-ebs-cleanup"},
                ],
            }
        ],
    )["SnapshotId"]

    results = apply_plan(plan, SETTINGS, execute=True, snapshot_first=True)

    assert first[0].status == "would_delete"
    assert results[0].snapshot_id == existing
    assert len(tool_snapshots(ec2, volume_id)) == 1


@mock_aws
def test_dry_run_with_snapshot_option_creates_no_snapshot():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()

    results = apply_plan(plan, SETTINGS, execute=False, snapshot_first=True)

    assert results[0].status == "would_delete"
    assert "snapshot" in results[0].detail
    assert tool_snapshots(ec2, volume_id) == []


@mock_aws
def test_volume_is_kept_when_the_snapshot_fails(monkeypatch):
    from ebs_cleanup import cleaner

    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    plan = make_plan()

    def broken(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "SnapshotLimitExceeded", "Message": "too many"}}, "CreateSnapshot"
        )

    monkeypatch.setattr(cleaner, "ensure_snapshot", broken)

    results = apply_plan(plan, SETTINGS, execute=True, snapshot_first=True)

    assert statuses(results) == {volume_id: "snapshot_failed"}
    assert volume_exists(ec2, volume_id)


@mock_aws
def test_other_account_without_a_role_is_reported_as_error():
    plan = make_plan()
    plan["items"] = [
        {
            "account_id": "999999999999",
            "region": "us-east-1",
            "volume_id": "vol-0abc",
            "created": "2026-01-01T00:00:00Z",
            "action": "delete",
        }
    ]

    results = apply_plan(plan, SETTINGS, execute=True)

    assert [r.status for r in results] == ["error"]
    assert "--role-name" in results[0].detail


@pytest.mark.parametrize("execute", [False, True])
@mock_aws
def test_empty_plan_does_nothing(execute):
    plan = make_plan()
    assert apply_plan(plan, SETTINGS, execute=execute) == []


# ---------------------------------------------------------------- the safety checks on their own
def fake_volume(**overrides):
    from datetime import datetime, timezone

    volume = {
        "State": "available",
        "Attachments": [],
        "CreateTime": datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        "Tags": [],
    }
    volume.update(overrides)
    return volume


PLAN_ITEM = {"created": "2026-08-01T12:00:00Z"}


def test_safe_volume_has_no_reason_to_skip():
    from ebs_cleanup.cleaner import reason_to_skip

    assert reason_to_skip(fake_volume(), PLAN_ITEM, []) is None


@pytest.mark.parametrize(
    ("volume", "expected"),
    [
        (fake_volume(State="in-use"), "skipped_not_available"),
        (fake_volume(State="deleting"), "skipped_not_available"),
        (fake_volume(Attachments=[{"InstanceId": "i-1"}]), "skipped_attached"),
        (fake_volume(Tags=[{"Key": "keep", "Value": "1"}]), "skipped_protected"),
    ],
)
def test_each_safety_check_blocks_deletion(volume, expected):
    from ebs_cleanup.cleaner import reason_to_skip

    status, _detail = reason_to_skip(volume, PLAN_ITEM, [("keep", None)])
    assert status == expected


def test_a_different_creation_time_blocks_deletion():
    from ebs_cleanup.cleaner import reason_to_skip

    status, _detail = reason_to_skip(fake_volume(), {"created": "2025-01-01T00:00:00Z"}, [])
    assert status == "skipped_changed"
