import boto3
from moto import mock_aws

from ebs_cleanup.models import Account
from ebs_cleanup.scanner import (
    K8S_PVC_TAG,
    find_unattached,
    list_regions,
    parse_protect_tags,
    protection_reason,
)
from tests.helpers import attach_to_new_instance, create_volume, days_from_now, make_settings

ACCOUNT = Account("123456789012", "test")


def scan(ec2, days_ahead=40, **settings):
    return find_unattached(ec2, ACCOUNT, "us-east-1", make_settings(**settings), days_from_now(days_ahead))


@mock_aws
def test_old_unattached_volume_is_found():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2, size=50, tags={"Name": "orphan"})

    found = scan(ec2)

    assert [f.volume_id for f in found] == [volume_id]
    assert found[0].age_days == 40
    assert found[0].size_gb == 50
    assert found[0].tags == {"Name": "orphan"}


@mock_aws
def test_young_volume_is_ignored():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    create_volume(ec2)

    assert scan(ec2, days_ahead=5) == []


@mock_aws
def test_volume_exactly_at_the_limit_is_included():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    create_volume(ec2)

    assert len(scan(ec2, days_ahead=30, min_age_days=30)) == 1


@mock_aws
def test_attached_volume_is_ignored():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    attach_to_new_instance(ec2, volume_id)

    assert scan(ec2) == []


@mock_aws
def test_only_the_unattached_one_is_reported():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    attached = create_volume(ec2)
    orphan = create_volume(ec2)
    attach_to_new_instance(ec2, attached)

    assert [f.volume_id for f in scan(ec2)] == [orphan]


@mock_aws
def test_protected_tags_are_flagged_not_hidden():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    keep = create_volume(ec2, tags={"keep": "yes"})
    finance = create_volume(ec2, tags={"owner": "finance"})
    plain = create_volume(ec2, tags={"owner": "eng"})

    found = {f.volume_id: f for f in scan(ec2, protect_tags=["keep", "owner=finance"])}

    assert found[keep].protected and found[keep].protect_reason == "tag keep"
    assert found[finance].protected and found[finance].protect_reason == "tag owner=finance"
    assert not found[plain].protected


@mock_aws
def test_kubernetes_tag_is_kept_in_tags():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    create_volume(ec2, tags={K8S_PVC_TAG: "data-postgres-0"})

    assert scan(ec2)[0].tags[K8S_PVC_TAG] == "data-postgres-0"


@mock_aws
def test_list_regions_returns_enabled_regions():
    regions = list_regions(boto3.client("ec2", region_name="us-east-1"))
    assert "us-east-1" in regions and "us-west-2" in regions


def test_parse_protect_tags():
    assert parse_protect_tags(["keep", " owner = finance "]) == [("keep", None), ("owner", "finance")]
    assert parse_protect_tags([]) == []


def test_protection_reason_needs_exact_value_when_given():
    rules = [("owner", "finance")]
    assert protection_reason({"owner": "finance"}, rules)
    assert protection_reason({"owner": "eng"}, rules) == ""
    assert protection_reason({}, rules) == ""
