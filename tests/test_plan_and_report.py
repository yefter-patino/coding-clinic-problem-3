import csv
import json

import pytest

from ebs_cleanup.models import ScanError
from ebs_cleanup.plan import build_plan, check_plan_age, load_plan, save_plan
from ebs_cleanup.report import write_errors_csv, write_findings_csv, write_summary
from tests.helpers import days_from_now, make_finding, make_result


def sample_result(**kwargs):
    findings = [
        make_finding(volume_id="vol-0001", monthly_cost=8.0),
        make_finding(
            volume_id="vol-0002",
            size_gb=1000,
            volume_type="io2",
            monthly_cost=300.0,
            region="us-west-2",
            tags={"Name": "big", "keep": "yes", "kubernetes.io/created-for/pvc/name": "data-0"},
            protected=True,
            protect_reason="tag keep",
        ),
    ]
    return make_result(findings, report_tags=["Name", "Owner"], protect_tags=["keep"], **kwargs)


# ---------------------------------------------------------------- plan
def test_plan_marks_protected_volumes_as_skip():
    plan = build_plan(sample_result())

    actions = {i["volume_id"]: i["action"] for i in plan["items"]}
    assert actions == {"vol-0001": "delete", "vol-0002": "skip"}
    assert plan["totals"] == {"delete_count": 1, "skip_count": 1, "estimated_monthly_savings_usd": 8.0}
    assert plan["parameters"]["protect_tags"] == ["keep"]


def test_plan_survives_save_and_load(tmp_path):
    plan = build_plan(sample_result())
    path = tmp_path / "plan.json"

    save_plan(plan, str(path))

    assert load_plan(str(path)) == plan


def test_load_plan_rejects_unknown_version(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"plan_version": 99, "items": []}))
    with pytest.raises(ValueError, match="plan_version"):
        load_plan(str(path))


def test_load_plan_rejects_missing_fields_and_bad_actions(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"plan_version": 1, "items": [{"volume_id": "vol-1"}]}))
    with pytest.raises(ValueError, match="missing"):
        load_plan(str(path))

    good = build_plan(sample_result())
    good["items"][0]["action"] = "destroy"
    path.write_text(json.dumps(good))
    with pytest.raises(ValueError, match="action"):
        load_plan(str(path))


def test_old_plans_are_refused():
    plan = build_plan(sample_result())
    plan["generated_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="more than 24 hours"):
        check_plan_age(plan, 24, days_from_now(0))


def test_fresh_plans_are_accepted():
    plan = build_plan(sample_result())
    plan["generated_at"] = days_from_now(0).strftime("%Y-%m-%dT%H:%M:%SZ")
    check_plan_age(plan, 24, days_from_now(0))


# ---------------------------------------------------------------- report
def test_findings_csv_has_one_row_per_volume_and_tag_columns(tmp_path):
    path = tmp_path / "out.csv"

    write_findings_csv(sample_result(), str(path))

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert [r["volume_id"] for r in rows] == ["vol-0001", "vol-0002"]
    assert rows[0]["est_monthly_cost_usd"] == "8.00"
    assert rows[0]["tag_Name"] == "orphan" and rows[0]["tag_Owner"] == ""
    assert rows[1]["protected"] == "True" and rows[1]["protect_reason"] == "tag keep"
    assert rows[1]["k8s_pvc"] == "data-0"


def test_summary_contains_totals_and_breakdowns(tmp_path):
    path = tmp_path / "summary.md"

    write_summary(sample_result(), str(path))

    text = path.read_text(encoding="utf-8")
    assert "$308.00" in text  # everything
    assert "Could be deleted | 1 | 100 | $8.00 | $96.00" in text
    assert "## By account" in text and "## By region" in text and "## By volume type" in text
    assert "vol-0002" in text  # in the top-ten table
    assert "created**, not from when it was detached" in text


def test_summary_lists_errors_and_handles_empty_results(tmp_path):
    errors = [ScanError("222222222222", "-", "connect", "AccessDenied", "nope")]
    path = tmp_path / "summary.md"

    write_summary(make_result([], errors=errors), str(path))

    text = path.read_text(encoding="utf-8")
    assert "No old unattached volumes were found." in text
    assert "NOT fully covered" in text and "222222222222" in text


def test_errors_csv(tmp_path):
    path = tmp_path / "errors.csv"
    write_errors_csv([ScanError("1", "us-east-1", "describe_volumes", "AuthFailure", "x")], str(path))
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert rows == [
        {
            "account_id": "1",
            "region": "us-east-1",
            "step": "describe_volumes",
            "code": "AuthFailure",
            "message": "x",
        }
    ]


def test_summary_explains_price_warnings_and_marks_protected_volumes(tmp_path):
    findings = [
        make_finding(volume_id="vol-0003", price_note="no prices for eu-west-1, used us-east-1"),
        make_finding(volume_id="vol-0004", protected=True, protect_reason="tag keep"),
    ]
    path = tmp_path / "summary.md"

    write_summary(make_result(findings), str(path))

    text = path.read_text(encoding="utf-8")
    assert "## Price warnings" in text
    assert "no prices for eu-west-1, used us-east-1" in text
    assert "| tag keep |" in text  # the protected column in the top-ten table
