# VoyagerAI sample data

Everything here is synthetic. The passengers are invented, the airline (XA) is fictional, and the labelled outcomes are
drawn from a known formula so that calibration has a truth to be checked against. `scripts/build_voyagerai_samples.py`
writes these files and writes the same files every time.

| File | What it is | Use it with |
|---|---|---|
| `passenger_elena_chen.json` | One rich profile with eleven dated, typed events | `sample: elena_chen`; any VoyagerAI journey |
| `passengers_sample.json` | 40 passengers for churn; four are deliberately too thin to score | `sample: passengers` |
| `disruption_manifest_sample.json` | 14 passengers of one cancelled flight | `sample: disruption`, `objective: disruption_sensitivity` |
| `churn_outcomes_sample.csv` | 2,400 labelled rows (11.8% churned), with gaps as real exports have | `action: calibrate`, `sample: churn_outcomes` |

The same shapes are what the agents read from a customer: see `docs/VOYAGERAI_PROPENSITY.md`.
