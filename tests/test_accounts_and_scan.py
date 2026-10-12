import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

from ebs_cleanup import scanner
from ebs_cleanup.accounts import list_organization_accounts, select_accounts
from ebs_cleanup.aws import make_session, session_for_account
from ebs_cleanup.models import Account
from ebs_cleanup.scanner import run_scan
from tests.helpers import create_volume, days_from_now, make_settings

ROLE = "yefter-ebs-audit"


def make_organization():
    """Make an organization with one member account. Returns the member's account id."""
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    status = org.create_account(AccountName="dev-account", Email="dev@example.com")
    return status["CreateAccountStatus"]["AccountId"]


def member_ec2(member_id):
    session = session_for_account(make_session(), member_id, ROLE, "aws", region="us-east-1")
    return session.client("ec2", region_name="us-east-1")


@mock_aws
def test_lists_active_accounts_of_the_organization():
    member_id = make_organization()

    accounts = list_organization_accounts(make_session())

    assert member_id in {a.account_id for a in accounts}
    assert len(accounts) == 2  # the management account plus the member


def test_select_accounts_include_and_exclude():
    accounts = [Account("111111111111", "a"), Account("222222222222", "b"), Account("333333333333", "c")]

    assert select_accounts(accounts, [], []) == accounts
    assert [a.name for a in select_accounts(accounts, ["222222222222"], [])] == ["b"]
    assert [a.name for a in select_accounts(accounts, [], ["222222222222"])] == ["a", "c"]
    assert select_accounts(accounts, ["999999999999"], []) == []  # unknown id is ignored (and logged)


@mock_aws
def test_current_scope_scans_only_this_account():
    member_id = make_organization()
    mine = create_volume(boto3.client("ec2", region_name="us-east-1"))
    create_volume(member_ec2(member_id))

    result = run_scan(make_settings(scope="current"), now=days_from_now(40))

    assert [f.volume_id for f in result.findings] == [mine]
    assert result.errors == []
    assert result.regions_scanned == 1


@mock_aws
def test_organization_scope_scans_every_account():
    member_id = make_organization()
    mine = create_volume(boto3.client("ec2", region_name="us-east-1"))
    theirs = create_volume(member_ec2(member_id), size=500, volume_type="gp2")

    result = run_scan(make_settings(scope="organization", role_name=ROLE), now=days_from_now(40))

    by_volume = {f.volume_id: f for f in result.findings}
    assert set(by_volume) == {mine, theirs}
    assert by_volume[theirs].account_id == member_id
    assert by_volume[theirs].account_name == "dev-account"
    assert by_volume[theirs].monthly_cost == 500 * 0.10  # gp2 price from config/prices.json
    assert result.regions_scanned == 2
    assert result.errors == []


@mock_aws
def test_exclude_accounts_is_respected():
    member_id = make_organization()
    mine = create_volume(boto3.client("ec2", region_name="us-east-1"))
    create_volume(member_ec2(member_id))

    result = run_scan(
        make_settings(scope="organization", role_name=ROLE, exclude_accounts=[member_id]),
        now=days_from_now(40),
    )

    assert [f.volume_id for f in result.findings] == [mine]


@mock_aws
def test_one_account_failing_is_reported_and_does_not_stop_the_scan(monkeypatch):
    member_id = make_organization()
    mine = create_volume(boto3.client("ec2", region_name="us-east-1"))

    def deny(*args, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "not allowed"}}, "AssumeRole")

    monkeypatch.setattr(scanner, "session_for_account", deny)

    result = run_scan(make_settings(scope="organization", role_name=ROLE), now=days_from_now(40))

    assert [f.volume_id for f in result.findings] == [mine]
    assert len(result.errors) == 1
    assert result.errors[0].account_id == member_id
    assert result.errors[0].code == "AccessDenied"
    assert result.errors[0].step == "connect"


@mock_aws
def test_one_region_failing_is_reported_and_other_regions_still_scan(monkeypatch):
    create_volume(boto3.client("ec2", region_name="us-east-1"))
    real = scanner.find_unattached

    def flaky(ec2, account, region, settings, now):
        if region == "us-west-2":
            raise ClientError(
                {"Error": {"Code": "AuthFailure", "Message": "region disabled"}}, "DescribeVolumes"
            )
        return real(ec2, account, region, settings, now)

    monkeypatch.setattr(scanner, "find_unattached", flaky)

    result = run_scan(make_settings(regions=["us-east-1", "us-west-2"]), now=days_from_now(40))

    assert len(result.findings) == 1
    assert [(e.region, e.code) for e in result.errors] == [("us-west-2", "AuthFailure")]
    assert result.regions_scanned == 1


@mock_aws
def test_all_enabled_regions_are_scanned_when_none_given():
    create_volume(boto3.client("ec2", region_name="us-east-1"))
    create_volume(boto3.client("ec2", region_name="us-west-2"), zone="us-west-2a")

    result = run_scan(make_settings(regions=[]), now=days_from_now(40))

    assert {f.region for f in result.findings} == {"us-east-1", "us-west-2"}
    assert result.regions_scanned > 2
