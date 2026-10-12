# yefter-ebs-unattached-volumes

A Python (boto3) tool that finds **unattached EBS volumes older than N days** in one AWS account or in
**every account of an AWS Organization**, writes a **cost report**, and can produce a **deletion plan**
that a person reviews before anything is deleted.

Unattached volumes (state `available`) are a classic cloud-cost leak: nothing uses them, but AWS still
bills for the storage and for any provisioned IOPS or throughput.

## Problem statement

Large organizations accumulate forgotten volumes in dozens of accounts and regions: leftovers from
terminated instances, failed deployments, old Kubernetes volume claims. Nobody owns the clean-up, and
deleting by hand is risky. This tool answers three questions safely:

1. Which unattached volumes are older than N days, in which accounts and regions?
2. What do they cost per month and per year?
3. If we delete them, exactly which volumes are in the plan, and did anything change since we looked?

## Architecture

```
 your laptop / CI job
 +------------------------------------------------------+
 |  python -m ebs_cleanup scan                          |
 |   1. who am I?                    (STS)              |
 |   2. which accounts?              (Organizations)    |
 |   3. per account, in parallel:                       |
 |        assume <prefix>-ebs-audit  (STS)              |
 |        per enabled region: DescribeVolumes           |
 |        keep: state=available AND age >= N days       |
 |   4. price each volume            (config/prices.json)|
 |   5. write report (+ plan)                           |
 +--------------------+---------------------------------+
                      |
        reports/  unattached-volumes-<time>.csv
                  summary-<time>.md
                  scan-errors-<time>.csv        (only if something failed)
                  deletion-plan-<time>.json     (only with --write-plan)
                      |
                 a human reviews the plan
                      |
 +--------------------v---------------------------------+
 |  python -m ebs_cleanup apply --plan <plan file>      |
 |   dry run by default; --execute really deletes       |
 |   re-checks every volume right before acting         |
 |   optional snapshot first, then DeleteVolume         |
 |   assumes <prefix>-ebs-cleanup (a different role)    |
 +------------------------------------------------------+
```

## Prerequisites

- **Python 3.10 or newer** (boto3 requires it). Check with `python3 --version`; on a Mac, if it is
  older, install a newer one, for example `brew install python`.
- AWS credentials that can run the commands below (see [IAM setup](#iam-setup)).
- For organization scope: access to the management account (or a delegated administrator) and the roles
  from `iam/member-account-roles.yaml` in the member accounts.

## Quick start (one account, good for a lab)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export AWS_PROFILE=<your-profile>             # or pass --profile
export AWS_DEFAULT_REGION=<your-home-region>  # or pass --home-region

python -m ebs_cleanup scan --min-age-days 30 --write-plan
```

The tool is read-only until you run `apply --execute`. It scans every region enabled in the account;
use `--regions <region>,<region>` to limit that.

## Usage

### Scan one account

```bash
python -m ebs_cleanup scan --min-age-days 30
```

### Scan a whole organization

```bash
python -m ebs_cleanup scan \
  --scope organization \
  --role-name <prefix>-ebs-audit \
  --min-age-days 30 \
  --protect-tags do-not-delete,retain=true \
  --report-tags Name,Owner,Project \
  --write-plan
```

### Review, then dry-run the plan

Open `reports/deletion-plan-<time>.json`. To keep a volume, change its `"action"` to `"skip"` (or delete
the entry). Then:

```bash
python -m ebs_cleanup apply --plan reports/deletion-plan-<time>.json \
  --role-name <prefix>-ebs-cleanup
```

This is a **dry run**: AWS is asked "would this delete work?" (`DryRun=True`) and nothing changes.

### Really delete (with a safety snapshot)

```bash
python -m ebs_cleanup apply --plan reports/deletion-plan-<time>.json \
  --role-name <prefix>-ebs-cleanup \
  --snapshot-before-delete --execute
```

You are asked to type `DELETE`. Automation can add `--yes` to skip the question.

### Settings from environment variables

Every option has an `EBS_CLEANUP_<OPTION>` environment variable, so scheduled jobs need no long command
lines. See [`config/env.example`](config/env.example).

### Options

| Option | Commands | Meaning |
|---|---|---|
| `--min-age-days N` | scan | Required. Only volumes created at least N days ago |
| `--scope current\|organization` | scan | This account only (default) or every active member account |
| `--role-name` | scan, apply | Role to assume in other accounts. Required for organization scope |
| `--external-id` | scan, apply | ExternalId, if the roles require one |
| `--regions a,b` | scan | Limit regions. Default: every region enabled in the account |
| `--include-accounts` / `--exclude-accounts` | scan | Comma-separated 12-digit account ids |
| `--protect-tags` | scan | Tags that keep a volume out of the plan: `key` or `key=value` |
| `--report-tags` | scan | Tag keys shown as CSV columns (default `Name`) |
| `--prices-file` | scan | Price list, default `config/prices.json` |
| `--workers` | scan | Accounts scanned in parallel (default 4) |
| `--write-plan` | scan | Also write the deletion plan |
| `--plan` | apply | The plan file. Required |
| `--execute` | apply | Really delete. Without it, dry run |
| `--yes` | apply | Skip the typed confirmation |
| `--snapshot-before-delete` | apply | Snapshot each volume and wait for it before deleting |
| `--snapshot-wait-minutes` | apply | Give up on a snapshot after this long (default 60) |
| `--max-plan-age-hours` | apply | Refuse older plans (default 24) |
| `--profile`, `--home-region` | both | AWS profile, and the region used for STS and listing regions |
| `--name-prefix` | both | Prefix for the session name and snapshots (default `yefter`) |
| `--output-dir` | both | Where files are written (default `reports`) |
| `-v` | both | Show progress per account and region |

Exit codes: `0` success, `1` error or cancelled, `2` finished but some accounts, regions or volumes
had problems (so a scheduled job can alert on it).

## What you get

| File | Contents |
|---|---|
| `unattached-volumes-<time>.csv` | One row per volume: account, region, id, size, type, IOPS, age, estimated monthly cost, protection, tag columns |
| `summary-<time>.md` | Totals, breakdown by account, region and type, ten most expensive volumes, price warnings, errors |
| `scan-errors-<time>.csv` | Only if something failed: which account or region was not scanned, and why |
| `deletion-plan-<time>.json` | Only with `--write-plan`: the reviewed-by-a-human list of volumes to delete |
| `apply-results-<time>.csv` | What `apply` did to each volume |

Synthetic examples, produced against a mock three-account organization, are in
[`examples/sample-output/`](examples/sample-output/).

## Safety design

Deleting data is the risky part, so the tool is built to be hard to misuse.

| Protection | How |
|---|---|
| Read-only by default | `scan` never changes anything. `apply` is a dry run unless you add `--execute` |
| Human in the loop | The plan is a file you read and edit. `apply` only touches items marked `delete` |
| Typed confirmation | `--execute` asks you to type `DELETE` (use `--yes` in automation) |
| Fresh plans only | Plans older than 24 hours are refused (`--max-plan-age-hours`) |
| Re-check before acting | For each volume, right before deleting: still exists? still `available`? same creation time as in the plan? no protected tag added since? |
| Protected tags | Volumes with `--protect-tags` tags appear in the report but are marked `skip` in the plan |
| Safety snapshot | `--snapshot-before-delete` waits for a finished snapshot. If the snapshot fails or times out, the volume is **not** deleted |
| AWS has the last word | `DeleteVolume` fails for a volume in use, whatever the plan says |
| Separate roles | Scanning uses a read-only role. Only `apply` uses the role that can delete |
| Nothing hidden | Accounts or regions that could not be scanned are listed in the summary and make the exit code `2` |

## IAM setup

### One account

Attach [`iam/single-account-audit-policy.json`](iam/single-account-audit-policy.json) to the user or
role that runs `scan`, and [`iam/single-account-cleanup-policy.json`](iam/single-account-cleanup-policy.json)
only if it should also run `apply`.

### An organization

1. **Member accounts.** Deploy [`iam/member-account-roles.yaml`](iam/member-account-roles.yaml) to every
   account (a CloudFormation StackSet from the management account is the usual way). It creates
   `<prefix>-ebs-audit` (read-only) and, only if you set `CreateCleanupRole=true`,
   `<prefix>-ebs-cleanup`.

   ```bash
   aws cloudformation deploy \
     --template-file iam/member-account-roles.yaml \
     --stack-name yefter-ebs-roles \
     --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides TrustedPrincipalArn=<arn-of-the-role-or-user-that-runs-the-tool> \
                           CreateCleanupRole=false
   ```

2. **Management account.** Deploy [`iam/runner-policy.yaml`](iam/runner-policy.yaml) and attach the
   managed policy to the user or role that runs the tool. It allows `organizations:ListAccounts` and
   `sts:AssumeRole` on the two role names only. A delegated administrator account for AWS Organizations
   can run the scan instead of the management account.

3. Use the same `--name-prefix` / `NamePrefix` everywhere so the role names match.

`OrganizationAccountAccessRole` also works with `--role-name`, but it is full administrator access.
Prefer the dedicated roles.

## Security considerations

- **Least privilege.** The audit role can only call `DescribeVolumes` and `DescribeRegions`. The cleanup
  role is optional, only exists where you create it, and can only snapshot and delete volumes.
- **Trust policy.** Roles can only be assumed by the principal you name, and optionally only with an
  ExternalId.
- **No stored secrets.** The tool uses your normal AWS credential chain (SSO, profile, environment). It
  never writes credentials to disk. Temporary role credentials live for one hour and only in memory.
- **Audit trail.** Every assumed session is named `<prefix>-ebs-cleanup`, so CloudTrail shows what the
  tool did and where.
- **Reports are internal data.** They contain account ids, volume ids and tag values. `reports/` is in
  `.gitignore`. A deletion plan is an instruction to delete, so store it like one.
- **Optional hard stop in IAM.** To make protected volumes undeletable even by this tool, add a deny
  statement to the cleanup role (replace the placeholder with your own protect tag key):

  ```json
  {
    "Effect": "Deny",
    "Action": "ec2:DeleteVolume",
    "Resource": "*",
    "Condition": { "Null": { "aws:ResourceTag/<YOUR_PROTECT_TAG_KEY>": "false" } }
  }
  ```

- **Snapshots cost money too.** A safety snapshot is billed until you delete it. The tool tags each one
  `CreatedBy=<prefix>-ebs-cleanup` and `SourceVolumeId=<volume id>` so you can find and expire them.

## Idempotency notes

- `scan` only reads. Running it twice gives the same report (rows are sorted the same way each time).
- `apply` can be run again after a partial run or a crash. Volumes that are already deleted are reported
  as `already_gone`, not as errors.
- A snapshot taken by an interrupted run is found again by its tags and reused instead of creating a
  second one.
- `apply` never edits the plan file.

## About the cost estimate

The cost per volume is `size x price per GB-month`, plus provisioned IOPS (gp3 above 3,000, io1, io2 in
tiers) and gp3 throughput above 125 MB/s. The prices come from [`config/prices.json`](config/prices.json),
which **you** maintain:

- The shipped file has **us-east-1 list prices only**, dated 2026-10-12, for gp3, gp2, io1, io2, st1 and
  sc1. Check them against the [AWS EBS pricing page](https://aws.amazon.com/ebs/pricing/) before relying
  on them.
- When the sample was prepared, AWS's own pages and a third-party summary were not fully consistent. The
  gp3 throughput price ($0.04 per MB/s-month) comes from AWS's volume-types page, one lookup of the
  pricing page showed a different figure, and the top io2 tier ($0.032) comes from a third-party
  summary. Confirm those numbers for your region.
- Regions or volume types that are not in the file are **flagged** in the CSV and summary instead of
  silently costing zero. Regions fall back to `default_region` prices. Unknown volume types
  (for example old `standard` magnetic volumes) count as $0 until you add a price.
- It is an estimate from list prices. Discounts, credits and your real bill are not included.

## Limitations

- **Age is time since creation, not since detachment.** EBS does not record when a volume was detached.
  A volume detached yesterday that was created a year ago will be listed. Check with the owner, or use
  `--protect-tags`, before deleting.
- Only volumes in state `available` are considered. Volumes in `error` state are not.
- Kubernetes volumes that are retained after their claim is deleted look like any other unattached
  volume. The CSV has a `k8s_pvc` column (from the `kubernetes.io/created-for/pvc/name` tag) to help you
  spot them.
- Role sessions last one hour. A very large account that takes longer than that to scan will report an
  error for the remaining regions.
- Snapshots of very large volumes can take hours. Raise `--snapshot-wait-minutes` or snapshot separately.
- **Tested against a simulated AWS (moto), not against real accounts.** Run `scan` and a dry-run `apply`
  in a sandbox account first.

## Testing and CI

```bash
pip install -r requirements-dev.txt
pytest -q                      # 70+ tests against a mock AWS, no credentials needed
ruff check . && ruff format --check .
cfn-lint iam/*.yaml
```

The tests cover the age filter, attached and protected volumes, cross-account scanning (including an
account that fails), costs for every volume type, plan validation, dry run vs execute, running a plan
twice, volumes that changed after the scan, and snapshot reuse.

GitHub Actions (`.github/workflows/ci.yml`) runs lint, formatting, JSON validation, `cfn-lint` and the
tests on Python 3.10, 3.12 and 3.13 for every pull request.

## Project layout

```
ebs_cleanup/
  cli.py        command line (scan, apply)
  scanner.py    accounts x regions, finds unattached volumes
  accounts.py   Organizations account list and filters
  aws.py        sessions and assume-role
  pricing.py    cost estimate
  report.py     CSV and Markdown report
  plan.py       deletion plan: build, save, load, validate
  cleaner.py    apply a plan safely
  models.py, config.py
config/         prices.json, env.example
iam/            CloudFormation roles, runner policy, single-account policies
examples/       sample output (synthetic)
tests/          pytest + moto
```

## Troubleshooting

| Message | What to do |
|---|---|
| `no region found` | Pass `--home-region` or set `AWS_DEFAULT_REGION` |
| `AccessDenied` when assuming a role | Check the role's trust policy, the runner policy, and `--external-id` |
| An account or region listed in `scan-errors` | The role is missing there, or the region is not enabled for that account |
| `no prices for <region>` / `no price for volume type` | Add the region or type to `config/prices.json` |
| `The plan was created ... more than 24 hours ago` | Run `scan --write-plan` again |
