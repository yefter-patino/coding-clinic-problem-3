"""The deletion plan: a JSON file a human reviews BEFORE anything is deleted."""

import json
from datetime import datetime, timedelta

PLAN_VERSION = 1
REQUIRED_ITEM_KEYS = ("account_id", "region", "volume_id", "created", "action")


def build_plan(result):
    """Turn scan findings into a plan. Protected volumes are listed but marked 'skip'."""
    s = result.settings
    items = []
    for f in result.findings:
        items.append(
            {
                "account_id": f.account_id,
                "account_name": f.account_name,
                "region": f.region,
                "volume_id": f.volume_id,
                "size_gb": f.size_gb,
                "volume_type": f.volume_type,
                "created": f.created,
                "age_days": f.age_days,
                "est_monthly_cost_usd": round(f.monthly_cost, 2),
                "action": "skip" if f.protected else "delete",
                "reason": f.protect_reason
                if f.protected
                else f"unattached and at least {s.min_age_days} days old",
            }
        )
    to_delete = [i for i in items if i["action"] == "delete"]
    return {
        "plan_version": PLAN_VERSION,
        "generated_at": result.started,
        "generated_by": result.identity_arn,
        "parameters": {
            "min_age_days": s.min_age_days,
            "scope": s.scope,
            "protect_tags": s.protect_tags,
        },
        "totals": {
            "delete_count": len(to_delete),
            "skip_count": len(items) - len(to_delete),
            "estimated_monthly_savings_usd": round(sum(i["est_monthly_cost_usd"] for i in to_delete), 2),
        },
        "instructions": (
            "Review every item. To keep a volume, change its action to 'skip' (or delete the item). "
            "Then run:  python -m ebs_cleanup apply --plan <this file>  (dry run by default)."
        ),
        "items": items,
    }


def save_plan(plan, path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=2)
        handle.write("\n")


def load_plan(path):
    """Read a plan and check it looks right. Raises ValueError with a clear message if not."""
    with open(path, encoding="utf-8") as handle:
        plan = json.load(handle)
    if plan.get("plan_version") != PLAN_VERSION:
        raise ValueError(f"{path}: unsupported plan_version {plan.get('plan_version')!r}")
    if not isinstance(plan.get("items"), list):
        raise ValueError(f"{path}: the plan has no 'items' list")
    for number, item in enumerate(plan["items"], start=1):
        missing = [key for key in REQUIRED_ITEM_KEYS if key not in item]
        if missing:
            raise ValueError(f"{path}: item {number} is missing {missing}")
        if item["action"] not in ("delete", "skip"):
            raise ValueError(f"{path}: item {number} has action {item['action']!r}")
    return plan


def check_plan_age(plan, max_hours, now):
    """Refuse old plans: volumes may have been attached or changed since the scan."""
    generated = datetime.strptime(plan["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=now.tzinfo)
    if now - generated > timedelta(hours=max_hours):
        raise ValueError(
            f"The plan was created at {plan['generated_at']}, more than {max_hours} hours ago. "
            "Run a new scan to get a fresh plan."
        )
