import pytest

from ebs_cleanup.pricing import estimate_monthly_cost, load_prices, tiered_cost
from tests.helpers import PRICES_FILE

# Small made-up price table so the expected numbers are easy to check by hand.
PRICES = {
    "default_region": "us-east-1",
    "regions": {
        "us-east-1": {
            "gp3": {
                "gb_month": 0.08,
                "free_iops": 3000,
                "iops_month": 0.005,
                "free_mbps": 125,
                "mbps_month": 0.04,
            },
            "gp2": {"gb_month": 0.10},
            "io1": {"gb_month": 0.125, "iops_month": 0.065},
            "io2": {
                "gb_month": 0.125,
                "iops_tiers": [
                    {"up_to": 32000, "price": 0.065},
                    {"up_to": 64000, "price": 0.046},
                    {"up_to": None, "price": 0.032},
                ],
            },
        }
    },
}


def test_gp3_baseline_has_no_extra_charges():
    cost, note = estimate_monthly_cost(PRICES, "us-east-1", "gp3", 100, 3000, 125)
    assert cost == pytest.approx(8.0)
    assert note == ""


def test_gp3_charges_for_extra_iops_and_throughput():
    cost, _ = estimate_monthly_cost(PRICES, "us-east-1", "gp3", 100, 4000, 250)
    assert cost == pytest.approx(8.0 + 1000 * 0.005 + 125 * 0.04)


def test_gp2_is_size_only():
    cost, _ = estimate_monthly_cost(PRICES, "us-east-1", "gp2", 200, 600, None)
    assert cost == pytest.approx(20.0)


def test_io1_charges_all_provisioned_iops():
    cost, _ = estimate_monthly_cost(PRICES, "us-east-1", "io1", 100, 1000, None)
    assert cost == pytest.approx(12.5 + 1000 * 0.065)


def test_io2_uses_tiers():
    cost, _ = estimate_monthly_cost(PRICES, "us-east-1", "io2", 100, 40000, None)
    assert cost == pytest.approx(12.5 + 32000 * 0.065 + 8000 * 0.046)


def test_io2_top_tier():
    cost, _ = estimate_monthly_cost(PRICES, "us-east-1", "io2", 100, 70000, None)
    assert cost == pytest.approx(12.5 + 32000 * 0.065 + 32000 * 0.046 + 6000 * 0.032)


def test_tiered_cost_zero_and_exact_boundary():
    tiers = PRICES["regions"]["us-east-1"]["io2"]["iops_tiers"]
    assert tiered_cost(0, tiers) == 0
    assert tiered_cost(32000, tiers) == pytest.approx(32000 * 0.065)


def test_unknown_volume_type_costs_zero_and_is_flagged():
    cost, note = estimate_monthly_cost(PRICES, "us-east-1", "standard", 100)
    assert cost == 0
    assert "standard" in note


def test_unknown_region_falls_back_and_is_flagged():
    cost, note = estimate_monthly_cost(PRICES, "eu-west-1", "gp2", 100)
    assert cost == pytest.approx(10.0)
    assert "eu-west-1" in note and "us-east-1" in note


def test_shipped_prices_file_is_usable():
    prices = load_prices(PRICES_FILE)
    for volume_type in ("gp3", "gp2", "io1", "io2", "st1", "sc1"):
        cost, note = estimate_monthly_cost(prices, prices["default_region"], volume_type, 100, 3000, 125)
        assert cost > 0 and note == "", volume_type


def test_prices_file_without_regions_is_rejected(tmp_path):
    bad = tmp_path / "prices.json"
    bad.write_text('{"default_region": "us-east-1"}')
    with pytest.raises(ValueError, match="regions"):
        load_prices(str(bad))
