# Examples

`sample-output/` shows what the tool writes. **It is synthetic.** It was produced by running the real
commands against a mock AWS (moto) with a pretend organization of three accounts, so the account ids,
volume ids and the user name are fake.

| File | What it is |
|---|---|
| `console.txt` | What the `scan` and a dry-run `apply` print |
| `sample-report.csv` | The per-volume report |
| `sample-summary.md` | The summary, with totals, breakdowns and the price warning |
| `sample-deletion-plan.json` | The deletion plan (one item is `skip` because of the `do-not-delete` tag) |
| `sample-apply-dry-run.csv` | The result of the dry-run `apply` |

The real files get a timestamp in their names (for example `summary-20261012T154241Z.md`). They were
renamed here so the links stay stable.
