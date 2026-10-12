import glob
import json
import os

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from ebs_cleanup import scanner
from ebs_cleanup.cli import main
from tests.helpers import PRICES_FILE, create_volume


def scan_args(out_dir, *extra):
    return ["scan", "--min-age-days", "0", "--prices-file", PRICES_FILE, "--output-dir", str(out_dir), *extra]


def files(out_dir, pattern):
    return glob.glob(os.path.join(str(out_dir), pattern))


def volume_exists(ec2, volume_id):
    try:
        ec2.describe_volumes(VolumeIds=[volume_id])
        return True
    except ClientError:
        return False


@mock_aws
def test_scan_writes_report_summary_and_plan(tmp_path, capsys):
    create_volume(boto3.client("ec2", region_name="us-east-1"), size=100)

    code = main(scan_args(tmp_path, "--regions", "us-east-1", "--write-plan"))

    assert code == 0
    assert len(files(tmp_path, "unattached-volumes-*.csv")) == 1
    assert len(files(tmp_path, "summary-*.md")) == 1
    assert len(files(tmp_path, "deletion-plan-*.json")) == 1
    assert files(tmp_path, "scan-errors-*.csv") == []
    assert "Found 1 old unattached volume" in capsys.readouterr().out


@mock_aws
def test_scan_without_write_plan_makes_no_plan(tmp_path):
    create_volume(boto3.client("ec2", region_name="us-east-1"))

    assert main(scan_args(tmp_path, "--regions", "us-east-1")) == 0
    assert files(tmp_path, "deletion-plan-*.json") == []


def test_min_age_days_is_required(tmp_path):
    with pytest.raises(SystemExit) as stop:
        main(["scan", "--output-dir", str(tmp_path)])
    assert stop.value.code == 2


def test_organization_scope_needs_a_role_name(tmp_path):
    with pytest.raises(SystemExit) as stop:
        main(scan_args(tmp_path, "--scope", "organization"))
    assert stop.value.code == 2


def test_bad_account_id_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as stop:
        main(scan_args(tmp_path, "--exclude-accounts", "12345"))
    assert stop.value.code == 2


@mock_aws
def test_settings_can_come_from_environment_variables(tmp_path, monkeypatch):
    create_volume(boto3.client("ec2", region_name="us-east-1"))
    monkeypatch.setenv("EBS_CLEANUP_MIN_AGE_DAYS", "0")
    monkeypatch.setenv("EBS_CLEANUP_REGIONS", "us-east-1")
    monkeypatch.setenv("EBS_CLEANUP_PRICES_FILE", PRICES_FILE)
    monkeypatch.setenv("EBS_CLEANUP_OUTPUT_DIR", str(tmp_path))

    assert main(["scan"]) == 0
    assert len(files(tmp_path, "unattached-volumes-*.csv")) == 1


@mock_aws
def test_scan_exits_with_2_when_part_of_it_failed(tmp_path, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise ClientError({"Error": {"Code": "AuthFailure", "Message": "nope"}}, "DescribeVolumes")

    monkeypatch.setattr(scanner, "find_unattached", broken)

    code = main(scan_args(tmp_path, "--regions", "us-east-1"))

    assert code == 2
    assert len(files(tmp_path, "scan-errors-*.csv")) == 1
    assert "could NOT be scanned" in capsys.readouterr().out


@mock_aws
def test_apply_is_a_dry_run_unless_execute_is_given(tmp_path, capsys):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    main(scan_args(tmp_path, "--regions", "us-east-1", "--write-plan"))
    plan_file = files(tmp_path, "deletion-plan-*.json")[0]

    code = main(["apply", "--plan", plan_file, "--output-dir", str(tmp_path)])

    assert code == 0
    assert volume_exists(ec2, volume_id)
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "would_delete: 1" in out


@mock_aws
def test_apply_execute_with_yes_deletes_and_writes_results(tmp_path):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    main(scan_args(tmp_path, "--regions", "us-east-1", "--write-plan"))
    plan_file = files(tmp_path, "deletion-plan-*.json")[0]

    code = main(["apply", "--plan", plan_file, "--execute", "--yes", "--output-dir", str(tmp_path)])

    assert code == 0
    assert not volume_exists(ec2, volume_id)
    assert len(files(tmp_path, "apply-results-*.csv")) == 1


@mock_aws
def test_apply_asks_for_confirmation_and_cancels_on_anything_but_delete(tmp_path, monkeypatch):
    ec2 = boto3.client("ec2", region_name="us-east-1")
    volume_id = create_volume(ec2)
    main(scan_args(tmp_path, "--regions", "us-east-1", "--write-plan"))
    plan_file = files(tmp_path, "deletion-plan-*.json")[0]
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")

    code = main(["apply", "--plan", plan_file, "--execute", "--output-dir", str(tmp_path)])

    assert code == 1
    assert volume_exists(ec2, volume_id)


@mock_aws
def test_apply_refuses_an_old_plan(tmp_path, capsys):
    create_volume(boto3.client("ec2", region_name="us-east-1"))
    main(scan_args(tmp_path, "--regions", "us-east-1", "--write-plan"))
    plan_file = files(tmp_path, "deletion-plan-*.json")[0]
    plan = json.load(open(plan_file))
    plan["generated_at"] = "2020-01-01T00:00:00Z"
    json.dump(plan, open(plan_file, "w"))

    code = main(["apply", "--plan", plan_file, "--execute", "--yes", "--output-dir", str(tmp_path)])

    assert code == 1
    assert "more than 24 hours" in capsys.readouterr().err
