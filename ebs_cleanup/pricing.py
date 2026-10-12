"""Estimate what an idle EBS volume costs per month.

This uses list prices from a JSON file that YOU maintain (config/prices.json).
It is an estimate, not your bill: discounts, credits and Savings Plans are not included.
"""

import json


def load_prices(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    for key in ("default_region", "regions"):
        if key not in data:
            raise ValueError(f"{path}: the prices file must contain '{key}'")
    return data


def tiered_cost(amount, tiers):
    """Price something that gets cheaper in steps (used for io2 provisioned IOPS).

    tiers looks like [{"up_to": 32000, "price": 0.065}, {"up_to": null, "price": 0.032}]
    """
    total = 0.0
    start = 0
    for tier in tiers:
        end = tier["up_to"] if tier["up_to"] is not None else float("inf")
        in_this_tier = max(0, min(amount, end) - start)
        total += in_this_tier * tier["price"]
        start = end
        if amount <= end:
            break
    return total


def estimate_monthly_cost(prices, region, volume_type, size_gb, iops=None, throughput_mbps=None):
    """Return (dollars_per_month, note). The note is empty when nothing was missing."""
    notes = []
    price_region = region
    if region not in prices["regions"]:
        price_region = prices["default_region"]
        notes.append(f"no prices for {region}, used {price_region}")

    rates = prices["regions"].get(price_region, {}).get(volume_type)
    if rates is None:
        notes.append(f"no price for volume type {volume_type}, cost counted as 0")
        return 0.0, "; ".join(notes)

    cost = size_gb * rates.get("gb_month", 0)

    # Provisioned IOPS. gp3 includes some for free, io1 and io2 charge for all of it.
    iops = iops or 0
    if "iops_tiers" in rates:
        cost += tiered_cost(iops, rates["iops_tiers"])
    elif "iops_month" in rates:
        cost += max(0, iops - rates.get("free_iops", 0)) * rates["iops_month"]

    # Provisioned throughput (gp3 only).
    if "mbps_month" in rates:
        extra_mbps = max(0, (throughput_mbps or 0) - rates.get("free_mbps", 0))
        cost += extra_mbps * rates["mbps_month"]

    return cost, "; ".join(notes)
