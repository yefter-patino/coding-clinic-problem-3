"""Command line for the tool.

  python -m ebs_cleanup scan  --min-age-days 30 [--scope organization --role-name ...] [--write-plan]
  python -m ebs_cleanup apply --plan reports/deletion-plan-....json [--execute]

Every option can also be set with an environment variable named EBS_CLEANUP_<OPTION>
(for example EBS_CLEANUP_MIN_AGE_DAYS), which is handy for scheduled jobs.
"""

import argparse
import csv
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from ebs_cleanup import __version__
from ebs_cleanup.aws import make_session
from ebs_cleanup.cleaner import PROBLEM_STATUSES, apply_plan
from ebs_cleanup.config import ApplySettings, ScanSettings
from ebs_cleanup.plan import build_plan, check_plan_age, load_plan, save_plan
from ebs_cleanup.report import money, write_errors_csv, write_findings_csv, write_summary
from ebs_cleanup.scanner import run_scan

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2  # finished, but some accounts / regions / volumes had problems


# ---------------------------------------------------------------- small helpers
def env(name, default=""):
    return os.environ.get(f"EBS_CLEANUP_{name}", default)


def csv_list(text):
    return [part.strip() for part in text.split(",") if part.strip()]


def account_id_list(text):
    ids = csv_list(text)
    bad = [i for i in ids if not re.fullmatch(r"\d{12}", i)]
    if bad:
        raise argparse.ArgumentTypeError(f"not 12-digit account ids: {', '.join(bad)}")
    return ids


def name_prefix(text):
    if not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,40}", text):
        raise argparse.ArgumentTypeError("use letters, digits and + = , . @ _ - (max 40)")
    return text


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def home_region(args, parser):
    region = args.home_region or make_session(args.profile).region_name
    if not region:
        parser.error("no region found: pass --home-region or set AWS_DEFAULT_REGION")
    return region


def add_aws_options(parser):
    parser.add_argument("--profile", default=env("PROFILE"), help="AWS CLI profile to start from")
    parser.add_argument(
        "--home-region", default=env("HOME_REGION"), help="region for STS and listing regions"
    )
    parser.add_argument("--role-name", default=env("ROLE_NAME"), help="role to assume in each member account")
    parser.add_argument(
        "--external-id", default=env("EXTERNAL_ID"), help="ExternalId for that role, if it needs one"
    )
    parser.add_argument(
        "--name-prefix",
        type=name_prefix,
        default=env("NAME_PREFIX", "yefter"),
        help="prefix for the session name and snapshots we create (default: yefter)",
    )
    parser.add_argument("--output-dir", default=env("OUTPUT_DIR", "reports"), help="where files are written")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="show progress for every account and region"
    )


def build_parser():
    parser = argparse.ArgumentParser(prog="ebs_cleanup", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    scan = commands.add_parser("scan", help="find unattached volumes, write a cost report")
    add_aws_options(scan)
    scan.add_argument(
        "--min-age-days",
        type=int,
        default=env("MIN_AGE_DAYS") or None,
        help="only volumes at least this many days old (required)",
    )
    scan.add_argument(
        "--scope",
        choices=["current", "organization"],
        default=env("SCOPE", "current"),
        help="current = this account only (default), organization = all member accounts",
    )
    scan.add_argument(
        "--regions",
        type=csv_list,
        default=env("REGIONS"),
        help="comma-separated regions (default: every enabled region)",
    )
    scan.add_argument(
        "--include-accounts",
        type=account_id_list,
        default=env("INCLUDE_ACCOUNTS"),
        help="only these 12-digit account ids",
    )
    scan.add_argument(
        "--exclude-accounts",
        type=account_id_list,
        default=env("EXCLUDE_ACCOUNTS"),
        help="skip these 12-digit account ids",
    )
    scan.add_argument(
        "--protect-tags",
        type=csv_list,
        default=env("PROTECT_TAGS"),
        help="volumes with these tags are never put in the plan; 'key' or 'key=value'",
    )
    scan.add_argument(
        "--report-tags",
        type=csv_list,
        default=env("REPORT_TAGS", "Name"),
        help="tag keys to show as columns in the CSV (default: Name)",
    )
    scan.add_argument(
        "--prices-file",
        default=env("PRICES_FILE", "config/prices.json"),
        help="JSON file with EBS list prices",
    )
    scan.add_argument(
        "--workers",
        type=int,
        default=int(env("WORKERS", "4")),
        help="accounts scanned in parallel (default: 4)",
    )
    scan.add_argument("--write-plan", action="store_true", help="also write a deletion plan (JSON)")

    apply = commands.add_parser("apply", help="carry out a reviewed deletion plan (dry run by default)")
    add_aws_options(apply)
    apply.add_argument("--plan", required=True, help="the deletion-plan JSON file")
    apply.add_argument("--execute", action="store_true", help="really delete (without it nothing changes)")
    apply.add_argument("--yes", action="store_true", help="do not ask for confirmation (for automation)")
    apply.add_argument(
        "--snapshot-before-delete",
        action="store_true",
        help="take a snapshot of each volume and wait for it before deleting",
    )
    apply.add_argument(
        "--snapshot-wait-minutes",
        type=int,
        default=int(env("SNAPSHOT_WAIT_MINUTES", "60")),
        help="give up on a snapshot after this long (default: 60)",
    )
    apply.add_argument(
        "--max-plan-age-hours",
        type=int,
        default=int(env("MAX_PLAN_AGE_HOURS", "24")),
        help="refuse plans older than this (default: 24)",
    )
    return parser


# ---------------------------------------------------------------- scan
def run_scan_command(args, parser):
    if args.min_age_days is None or args.min_age_days < 0:
        parser.error("--min-age-days is required and must be 0 or more")
    if args.scope == "organization" and not args.role_name:
        parser.error("--role-name is required with --scope organization")

    settings = ScanSettings(
        min_age_days=args.min_age_days,
        scope=args.scope,
        role_name=args.role_name,
        external_id=args.external_id,
        profile=args.profile,
        home_region=home_region(args, parser),
        regions=args.regions,
        include_accounts=args.include_accounts,
        exclude_accounts=args.exclude_accounts,
        protect_tags=args.protect_tags,
        report_tags=args.report_tags,
        prices_file=args.prices_file,
        workers=args.workers,
        name_prefix=args.name_prefix,
    )
    result = run_scan(settings)

    os.makedirs(args.output_dir, exist_ok=True)
    stamp = timestamp()
    csv_path = os.path.join(args.output_dir, f"unattached-volumes-{stamp}.csv")
    summary_path = os.path.join(args.output_dir, f"summary-{stamp}.md")
    write_findings_csv(result, csv_path)
    write_summary(result, summary_path)
    written = [csv_path, summary_path]

    if result.errors:
        errors_path = os.path.join(args.output_dir, f"scan-errors-{stamp}.csv")
        write_errors_csv(result.errors, errors_path)
        written.append(errors_path)

    plan = None
    if args.write_plan:
        plan = build_plan(result)
        plan_path = os.path.join(args.output_dir, f"deletion-plan-{stamp}.json")
        save_plan(plan, plan_path)
        written.append(plan_path)

    protected = sum(1 for f in result.findings if f.protected)
    monthly = sum(f.monthly_cost for f in result.findings if not f.protected)
    print(f"Scanned {len(result.accounts)} account(s), {result.regions_scanned} account/region pair(s).")
    print(f"Found {len(result.findings)} old unattached volume(s), {protected} protected by tag.")
    print(
        f"Estimated cost of the unprotected ones: {money(monthly)} per month, {money(monthly * 12)} per year."
    )
    if result.errors:
        print(f"WARNING: {len(result.errors)} account/region(s) could NOT be scanned. See the errors file.")
    print("Files written:")
    for path in written:
        print(f"  {path}")
    if plan:
        print("Review the plan, then run:  python -m ebs_cleanup apply --plan <plan file>")
    return EXIT_PARTIAL if result.errors else EXIT_OK


# ---------------------------------------------------------------- apply
def run_apply_command(args, parser):
    now = datetime.now(timezone.utc)
    plan = load_plan(args.plan)
    check_plan_age(plan, args.max_plan_age_hours, now)

    to_delete = [i for i in plan["items"] if i["action"] == "delete"]
    if not to_delete:
        print("The plan has no volumes marked 'delete'. Nothing to do.")
        return EXIT_OK

    if args.execute and not args.yes:
        answer = input(f"This will PERMANENTLY delete {len(to_delete)} volume(s). Type DELETE to continue: ")
        if answer.strip() != "DELETE":
            print("Cancelled. Nothing was changed.")
            return EXIT_FATAL

    settings = ApplySettings(
        role_name=args.role_name,
        external_id=args.external_id,
        profile=args.profile,
        home_region=home_region(args, parser),
        name_prefix=args.name_prefix,
        snapshot_wait_minutes=args.snapshot_wait_minutes,
    )
    results = apply_plan(plan, settings, execute=args.execute, snapshot_first=args.snapshot_before_delete)

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, f"apply-results-{timestamp()}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["account_id", "region", "volume_id", "status", "detail", "snapshot_id"])
        for r in results:
            writer.writerow([r.account_id, r.region, r.volume_id, r.status, r.detail, r.snapshot_id])

    print("DRY RUN: nothing was changed. Add --execute to delete." if not args.execute else "EXECUTED.")
    for status, count in sorted(Counter(r.status for r in results).items()):
        print(f"  {status}: {count}")
    print(f"Results: {out_path}")
    return EXIT_PARTIAL if any(r.status in PROBLEM_STATUSES for r in results) else EXIT_OK


# ---------------------------------------------------------------- entry point
def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    try:
        if args.command == "scan":
            return run_scan_command(args, parser)
        return run_apply_command(args, parser)
    except (ClientError, BotoCoreError, ValueError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_FATAL
