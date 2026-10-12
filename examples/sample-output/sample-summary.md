# Unattached EBS volume report

- Generated: 2026-10-12T15:43:46Z
- Run by: `arn:aws:sts::123456789012:user/moto`
- Scope: organization, volumes unattached and at least 30 days old
- Accounts scanned: 3, account/region pairs scanned: 6, errors: 0
- Prices: `config/prices.json` (list prices dated 2026-10-12)

## Totals

|  | Volumes | Size (GiB) | Est. monthly cost | Est. yearly cost |
|---|---|---|---|---|
| All unattached and old | 7 | 4,350 | $1,647.00 | $19,764.00 |
| Protected by tag (kept) | 1 | 500 | $60.00 | $720.00 |
| Could be deleted | 6 | 3,850 | $1,587.00 | $19,044.00 |

## By account

| Account | Volumes | Size (GiB) | Est. monthly cost |
|---|---|---|---|
| 424396616856 (payments-prod) | 3 | 1,800 | $1,509.00 |
| 988264380294 (data-platform-dev) | 3 | 2,350 | $118.00 |
| 123456789012 (master) | 1 | 200 | $20.00 |

## By region

| Region | Volumes | Size (GiB) | Est. monthly cost |
|---|---|---|---|
| us-east-1 | 6 | 4,050 | $1,623.00 |
| us-west-2 | 1 | 300 | $24.00 |

## By volume type

| Type | Volumes | Size (GiB) | Est. monthly cost |
|---|---|---|---|
| io2 | 1 | 1,000 | $1,425.00 |
| gp3 | 4 | 1,150 | $112.00 |
| st1 | 1 | 2,000 | $90.00 |
| gp2 | 1 | 200 | $20.00 |

## Ten most expensive volumes

| Account | Region | Volume | Type | Size (GiB) | Age (days) | Est. monthly | Protected |
|---|---|---|---|---|---|---|---|
| 424396616856 | us-east-1 | vol-638d2793ed4c2f0a2 | io2 | 1000 | 95 | $1,425.00 |  |
| 988264380294 | us-east-1 | vol-e092efe7d8a4e8f0c | st1 | 2000 | 130 | $90.00 |  |
| 424396616856 | us-east-1 | vol-47af1eb917d987cd3 | gp3 | 500 | 400 | $60.00 | tag do-not-delete |
| 424396616856 | us-west-2 | vol-9ca09970e694a1f95 | gp3 | 300 | 61 | $24.00 |  |
| 123456789012 | us-east-1 | vol-2a4b27f46743466c9 | gp2 | 200 | 210 | $20.00 |  |
| 988264380294 | us-east-1 | vol-46195ff9a9a814b48 | gp3 | 250 | 75 | $20.00 |  |
| 988264380294 | us-east-1 | vol-d9a1f61781b924ab2 | gp3 | 100 | 48 | $8.00 |  |

## Price warnings

Some volumes could not be priced exactly. Add the missing regions or volume types to `config/prices.json` for a better estimate.

| Volumes | Note |
|---|---|
| 1 | no prices for us-west-2, used us-east-1 |

## How to read this

- Unattached means the volume state is `available`: no instance is using it, but you still pay for the storage and any provisioned IOPS or throughput.
- Age is counted from when the volume was **created**, not from when it was detached. A new detach of an old volume still shows up, so check with the owner.
- Costs are estimates from the list prices in the prices file, not from your bill.
