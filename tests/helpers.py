"""Small helpers shared by the tests."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ebs_cleanup.config import ScanSettings
from ebs_cleanup.models import Finding, ScanResult

PRICES_FILE = str(Path(__file__).resolve().parent.parent / "config" / "prices.json")


def days_from_now(days):
    """A time in the future. Moto creates volumes 'now', so we move the clock forward instead."""
    return datetime.now(timezone.utc) + timedelta(days=days)


def make_settings(**overrides):
    values = {
        "min_age_days": 30,
        "home_region": "us-east-1",
        "regions": ["us-east-1"],
        "prices_file": PRICES_FILE,
        "workers": 2,
    }
    values.update(overrides)
    return ScanSettings(**values)


def create_volume(ec2, size=10, volume_type="gp3", tags=None, zone="us-east-1a"):
    request = {"AvailabilityZone": zone, "Size": size, "VolumeType": volume_type}
    if tags:
        request["TagSpecifications"] = [
            {"ResourceType": "volume", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
        ]
    return ec2.create_volume(**request)["VolumeId"]


def attach_to_new_instance(ec2, volume_id):
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    instance = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1)["Instances"][0]
    ec2.attach_volume(VolumeId=volume_id, InstanceId=instance["InstanceId"], Device="/dev/sdf")
    return instance["InstanceId"]


def make_finding(**overrides):
    values = {
        "account_id": "123456789012",
        "account_name": "test",
        "region": "us-east-1",
        "volume_id": "vol-0001",
        "size_gb": 100,
        "volume_type": "gp3",
        "iops": 3000,
        "throughput_mbps": 125,
        "encrypted": True,
        "availability_zone": "us-east-1a",
        "source_snapshot_id": "",
        "created": "2026-08-01T12:00:00Z",
        "age_days": 70,
        "tags": {"Name": "orphan"},
        "monthly_cost": 8.0,
    }
    values.update(overrides)
    return Finding(**values)


def make_result(findings, errors=None, **setting_overrides):
    return ScanResult(
        settings=make_settings(**setting_overrides),
        identity_arn="arn:aws:iam::123456789012:user/tester",
        started="2026-10-12T15:00:00Z",
        accounts=[],
        findings=findings,
        errors=errors or [],
        regions_scanned=1,
        prices_meta={"price_list_date": "2026-10-12", "file": "config/prices.json"},
    )
