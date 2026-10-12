"""Write the cost report: a CSV with every volume and a short Markdown summary."""

import csv
from collections import Counter, defaultdict

from ebs_cleanup.scanner import K8S_PVC_TAG

BASE_COLUMNS = [
    "account_id",
    "account_name",
    "region",
    "volume_id",
    "size_gb",
    "volume_type",
    "iops",
    "throughput_mbps",
    "encrypted",
    "availability_zone",
    "source_snapshot_id",
    "created",
    "age_days",
    "est_monthly_cost_usd",
    "price_note",
    "protected",
    "protect_reason",
    "k8s_pvc",
]


def money(value):
    return f"${value:,.2f}"


def write_findings_csv(result, path):
    tag_columns = [f"tag_{key}" for key in result.settings.report_tags]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(BASE_COLUMNS + tag_columns)
        for f in result.findings:
            row = [
                f.account_id,
                f.account_name,
                f.region,
                f.volume_id,
                f.size_gb,
                f.volume_type,
                f.iops if f.iops is not None else "",
                f.throughput_mbps if f.throughput_mbps is not None else "",
                f.encrypted,
                f.availability_zone,
                f.source_snapshot_id,
                f.created,
                f.age_days,
                f"{f.monthly_cost:.2f}",
                f.price_note,
                f.protected,
                f.protect_reason,
                f.tags.get(K8S_PVC_TAG, ""),
            ]
            row += [f.tags.get(key, "") for key in result.settings.report_tags]
            writer.writerow(row)


def write_errors_csv(errors, path):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["account_id", "region", "step", "code", "message"])
        for e in errors:
            writer.writerow([e.account_id, e.region, e.step, e.code, e.message])


# ---------------------------------------------------------------- summary
def group_totals(findings, key_function):
    """{group: [volume_count, total_gb, monthly_cost]}"""
    totals = defaultdict(lambda: [0, 0, 0.0])
    for f in findings:
        row = totals[key_function(f)]
        row[0] += 1
        row[1] += f.size_gb
        row[2] += f.monthly_cost
    return totals


def markdown_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def breakdown_table(title, label, totals):
    rows = [
        (name, count, f"{gb:,}", money(cost))
        for name, (count, gb, cost) in sorted(totals.items(), key=lambda item: -item[1][2])
    ]
    table = markdown_table([label, "Volumes", "Size (GiB)", "Est. monthly cost"], rows)
    return f"## {title}\n\n{table}\n"


def write_summary(result, path):
    s = result.settings
    findings = result.findings
    deletable = [f for f in findings if not f.protected]
    protected = [f for f in findings if f.protected]
    monthly_all = sum(f.monthly_cost for f in findings)
    monthly_deletable = sum(f.monthly_cost for f in deletable)

    parts = [
        "# Unattached EBS volume report\n",
        f"- Generated: {result.started}",
        f"- Run by: `{result.identity_arn}`",
        f"- Scope: {s.scope}, volumes unattached and at least {s.min_age_days} days old",
        f"- Accounts scanned: {len(result.accounts)}, account/region pairs scanned: "
        f"{result.regions_scanned}, errors: {len(result.errors)}",
        f"- Prices: `{result.prices_meta['file']}` (list prices dated "
        f"{result.prices_meta['price_list_date']})\n",
        "## Totals\n",
        markdown_table(
            ["", "Volumes", "Size (GiB)", "Est. monthly cost", "Est. yearly cost"],
            [
                (
                    "All unattached and old",
                    len(findings),
                    f"{sum(f.size_gb for f in findings):,}",
                    money(monthly_all),
                    money(monthly_all * 12),
                ),
                (
                    "Protected by tag (kept)",
                    len(protected),
                    f"{sum(f.size_gb for f in protected):,}",
                    money(monthly_all - monthly_deletable),
                    money((monthly_all - monthly_deletable) * 12),
                ),
                (
                    "Could be deleted",
                    len(deletable),
                    f"{sum(f.size_gb for f in deletable):,}",
                    money(monthly_deletable),
                    money(monthly_deletable * 12),
                ),
            ],
        )
        + "\n",
    ]

    if findings:
        parts.append(
            breakdown_table(
                "By account",
                "Account",
                group_totals(findings, lambda f: f"{f.account_id} ({f.account_name})"),
            )
        )
        parts.append(breakdown_table("By region", "Region", group_totals(findings, lambda f: f.region)))
        parts.append(
            breakdown_table("By volume type", "Type", group_totals(findings, lambda f: f.volume_type))
        )
        top = sorted(findings, key=lambda f: -f.monthly_cost)[:10]
        parts.append(
            "## Ten most expensive volumes\n\n"
            + markdown_table(
                [
                    "Account",
                    "Region",
                    "Volume",
                    "Type",
                    "Size (GiB)",
                    "Age (days)",
                    "Est. monthly",
                    "Protected",
                ],
                [
                    (
                        f.account_id,
                        f.region,
                        f.volume_id,
                        f.volume_type,
                        f.size_gb,
                        f.age_days,
                        money(f.monthly_cost),
                        f.protect_reason or "",
                    )
                    for f in top
                ],
            )
            + "\n"
        )
    else:
        parts.append("No old unattached volumes were found.\n")

    notes = Counter(f.price_note for f in findings if f.price_note)
    if notes:
        parts.append(
            "## Price warnings\n\n"
            "Some volumes could not be priced exactly. Add the missing regions or volume types to "
            f"`{result.prices_meta['file']}` for a better estimate.\n\n"
            + markdown_table(["Volumes", "Note"], [(count, note) for note, count in notes.most_common()])
            + "\n"
        )

    if result.errors:
        parts.append(
            "## Errors (these accounts/regions are NOT fully covered)\n\n"
            + markdown_table(
                ["Account", "Region", "Step", "Code"],
                [(e.account_id, e.region, e.step, e.code) for e in result.errors],
            )
            + "\n\nDetails are in the errors CSV.\n"
        )

    parts.append(
        "## How to read this\n\n"
        "- Unattached means the volume state is `available`: no instance is using it, "
        "but you still pay for the storage and any provisioned IOPS or throughput.\n"
        "- Age is counted from when the volume was **created**, not from when it was detached. "
        "A new detach of an old volume still shows up, so check with the owner.\n"
        "- Costs are estimates from the list prices in the prices file, not from your bill.\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts))
