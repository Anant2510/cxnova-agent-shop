# VoyagerAI Predictive Propensity: what it predicts, from what, and how far to trust it

The Predictive Propensity Agent (`vai_predictive_propensity`) calculates a score in code. No model is asked for the number:
the same data gives the same score on any model, in any cloud and in any of the three run modes. This page is the reference
for a customer's data team. Everything here is also returned by the agent itself:

| Action | What it returns |
|---|---|
| `describe` | Each prediction, its parameters with their share of the weight, the minimum data, the decision bands, the limits |
| `check` | For the data you pass: which predictions it supports, how many records can be scored, what is missing (most important first) |
| `score` | The propensity for one passenger, or for a file of passengers with a `scores.csv` to download |
| `calibrate` | Weights fitted to your labelled outcomes, validated on a holdout and compared with the default |

## The data it takes

`profile` is one passenger (a JSON object) or many (a JSON list, NDJSON, or CSV with one row per passenger). In the portal
the field is a file picker with a template to download; over the API, MCP or A2A it is the object, the list, or the file's
text. A profile carries:

- an identifier: one of `passenger_id`, `pax_id`, `customer_id`, `member_id`, `loyalty_id`, `loyalty_number`, `ffn`, `pnr`,
  `id`, or failing those a `name`;
- the model's parameters under their own names (the tables below), for example `disruptions_28d: 3`;
- optionally `history`: dated events, each with a `type`: `disruption`, `competitor_browsing`, `redemption_decline`,
  `complaint`, `app_uninstall_intent`, `search`, `fare_page_view`, `cart_abandon`, `email_click`, `app_session`,
  `upgrade_accepted`, `upgrade_declined`, `booking`. Windowed parameters are counted from these (three `disruption`
  events in the 28 days to `as_of` give `disruptions_28d = 3`) and the result names the events counted;
- optionally `as_of` (the date the windows are counted back from) and `history_complete: true`, which says the history is
  the whole history for those windows, so an event type with no events counts as zero instead of unknown.

An empty CSV cell or an absent field is unknown. It is never read as zero.

Free-text signals ("3 disruptions in 28 days") are typed by rules and each typed event says which rule typed it. Events
with an explicit `type` are exact, and are what a production feed should send.

## The minimum, and what happens below it

Each prediction has a minimum: an identifier, at least three of its parameters, those parameters covering at least 40% of
the model's weight, and at least one of its core parameters. Below it the agent stops:

```
Churn propensity cannot be calculated for Elena Chen: 1 of the model's 11 parameters found in the data (at least 3
needed); what was found carries 3% of the model's weight (at least 40% needed); none of the core parameters was found
(one of: disruptions_28d, days_since_last_booking, booking_trend, competitor_browsing_28d). Add, most important first:
disruptions_28d, competitor_browsing_28d, booking_trend, days_since_last_booking, nps_last.
```

A name, a tier and a description of someone's mood are not data to predict from, so nothing is inferred from them. In a
file, each passenger is scored or refused on their own, and the refusals are listed with the reason.

Above the minimum, a missing parameter is left at its neutral value. It lowers `coverage` (the share of the model's weight
your data carried) and so the `confidence` (high from 75%, medium from 50%, otherwise low). It does not move the score.

## The predictions

### Churn propensity (`churn`)

The likelihood that an active loyalty member stops booking with the airline in the next 90 days. Unit: one passenger. Base rate used until calibrated: 12%.

Minimum to score: an identifier, at least 3 parameters, covering at least 40% of the model's weight, including one of `disruptions_28d`, `days_since_last_booking`, `booking_trend`, `competitor_browsing_28d`.

| Parameter | Field read | Effect | Share of weight | Range | Also counted from |
|---|---|---|---|---|---|
| Disruptions in the last 28 days | `disruptions_28d` | raises | 15% | 0 to 4 | disruption events in 28 days |
| Competitor fare views in the last 28 days | `competitor_browsing_28d` | raises | 14% | 0 to 3 | competitor_browsing events in 28 days |
| Bookings in the last 12 months against the 12 before (1.0 = unchanged) | `booking_trend` | lowers | 12% | 0 to 1.5 |  |
| Days since the last booking | `days_since_last_booking` | raises | 12% | 0 to 180 |  |
| Complaints in the last 90 days | `complaints_90d` | raises | 9% | 0 to 3 | complaint events in 90 days |
| Last NPS answer (0 to 10) | `nps_last` | lowers | 9% | 0 to 10 |  |
| App uninstall or unsubscribe intent | `app_uninstall_intent` | raises | 9% | 0 to 1 | app_uninstall_intent events in 90 days |
| Declined redemptions in the last 90 days | `redemption_declines_90d` | raises | 8% | 0 to 3 | redemption_decline events in 90 days |
| App sessions in the last 30 days | `app_sessions_30d` | lowers | 6% | 0 to 12 | app_session events in 30 days |
| Years as a member | `tenure_years` | lowers | 5% | 0 to 10 |  |
| Loyalty tier | `tier_level` | lowers | 3% | 0 to 3 |  |

| Score from | Decision | Priority | Act |
|---|---|---|---|
| 0.70 | RETAIN CRITICAL | HIGH | immediately |
| 0.45 | PROTECT | MEDIUM | within 7 days |
| 0.25 | WATCH | LOW | in the next campaign cycle |
| 0.00 | NO ACTION | NONE | no action needed |

Outcome column for calibration: `churned`.

### Disruption sensitivity (`disruption_sensitivity`)

The likelihood that the current disruption damages the relationship (a complaint, a compensation claim or lost future bookings) unless this passenger is handled first. Unit: one passenger (or the lead passenger of a booking). Base rate used until calibrated: 20%.

Minimum to score: an identifier, at least 3 parameters, covering at least 40% of the model's weight, including one of `delay_minutes`, `is_cancelled`, `missed_connection`.

| Parameter | Field read | Effect | Share of weight | Range | Also counted from |
|---|---|---|---|---|---|
| Delay of the current disruption (minutes) | `delay_minutes` | raises | 16% | 0 to 360 |  |
| The flight is cancelled | `is_cancelled` | raises | 14% | 0 to 1 |  |
| A connection is missed or at risk | `missed_connection` | raises | 12% | 0 to 1 |  |
| Disruptions in the last 28 days | `disruptions_28d` | raises | 11% | 0 to 4 | disruption events in 28 days |
| An overnight stay is needed | `overnight_required` | raises | 10% | 0 to 1 |  |
| Alternative flights available today | `rebook_options` | lowers | 9% | 0 to 5 |  |
| Children, reduced mobility or medical needs on the booking | `special_assistance` | raises | 9% | 0 to 1 |  |
| Loyalty tier | `tier_level` | raises | 7% | 0 to 3 |  |
| Travellers on the booking | `party_size` | raises | 6% | 1 to 6 |  |
| Travelling on business | `business_travel` | raises | 6% | 0 to 1 |  |

| Score from | Decision | Priority | Act |
|---|---|---|---|
| 0.80 | REBOOK FIRST | HIGH | immediately |
| 0.55 | PROACTIVE CARE | MEDIUM | within the hour |
| 0.30 | STANDARD HANDLING | LOW | in the normal queue |
| 0.00 | MONITOR | NONE | no action needed |

Outcome column for calibration: `adverse_outcome`.

### Conversion propensity (`conversion`)

The likelihood that an active shopper books the trip being searched within the next 7 days. Unit: one shopper and one searched trip. Base rate used until calibrated: 8%.

Minimum to score: an identifier, at least 3 parameters, covering at least 40% of the model's weight, including one of `searches_7d`, `fare_page_views_7d`, `cart_abandons_14d`.

| Parameter | Field read | Effect | Share of weight | Range | Also counted from |
|---|---|---|---|---|---|
| Flight searches in the last 7 days | `searches_7d` | raises | 16% | 0 to 8 | search events in 7 days |
| Abandoned bookings in the last 14 days | `cart_abandons_14d` | raises | 15% | 0 to 2 | cart_abandon events in 14 days |
| Fare page views in the last 7 days | `fare_page_views_7d` | raises | 14% | 0 to 6 | fare_page_view events in 7 days |
| Bookings in the last 12 months | `bookings_12m` | raises | 11% | 0 to 12 | booking events in 365 days |
| Days to departure of the trip in question | `days_to_departure` | lowers | 11% | 0 to 120 |  |
| Email clicks in the last 14 days | `email_clicks_14d` | raises | 10% | 0 to 4 | email_click events in 14 days |
| Competitor fare views in the last 28 days | `competitor_browsing_28d` | lowers | 10% | 0 to 3 | competitor_browsing events in 28 days |
| Saw a price drop on the searched route | `price_drop_seen` | raises | 8% | 0 to 1 |  |
| Browsing while logged in | `logged_in` | raises | 7% | 0 to 1 |  |

| Score from | Decision | Priority | Act |
|---|---|---|---|
| 0.55 | CLOSE NOW | HIGH | within 24 hours |
| 0.30 | NURTURE | MEDIUM | within 3 days |
| 0.12 | KEEP WARM | LOW | in the next campaign cycle |
| 0.00 | NO ACTION | NONE | no action needed |

Outcome column for calibration: `booked`.

### Upsell propensity (`upsell`)

The likelihood that the passenger accepts an upgrade or a paid ancillary offered for the trip in question. Unit: one passenger and one trip. Base rate used until calibrated: 10%.

Minimum to score: an identifier, at least 3 parameters, covering at least 40% of the model's weight, including one of `upgrades_12m`, `ancillary_spend_12m_usd`, `cabin_level`, `flight_hours`.

| Parameter | Field read | Effect | Share of weight | Range | Also counted from |
|---|---|---|---|---|---|
| Upgrades bought or accepted in the last 12 months | `upgrades_12m` | raises | 20% | 0 to 4 | upgrade_accepted events in 365 days |
| Ancillary spend in the last 12 months (USD) | `ancillary_spend_12m_usd` | raises | 14% | 0 to 1500 |  |
| Cabin booked | `cabin_level` | lowers | 14% | 0 to 3 |  |
| Length of the flight in question (hours) | `flight_hours` | raises | 12% | 1 to 14 |  |
| Upgrade offers declined in the last 12 months | `upgrade_offers_declined_12m` | lowers | 12% | 0 to 4 | upgrade_declined events in 365 days |
| Miles balance | `miles_balance` | raises | 9% | 0 to 150000 |  |
| Loyalty tier | `tier_level` | raises | 8% | 0 to 3 |  |
| Travelling on business | `business_travel` | raises | 6% | 0 to 1 |  |
| Days to departure of the trip in question | `days_to_departure` | lowers | 6% | 0 to 60 |  |

| Score from | Decision | Priority | Act |
|---|---|---|---|
| 0.50 | OFFER UPGRADE | HIGH | in this booking flow |
| 0.28 | OFFER ANCILLARY | MEDIUM | in the pre-trip email |
| 0.12 | SOFT PROMPT | LOW | at check-in |
| 0.00 | NO OFFER | NONE | no action needed |

Outcome column for calibration: `accepted_offer`.

## The method

A scorecard per prediction. Each parameter is placed on its range (`x = (value - low) / (high - low)`, held between 0 and
1), multiplied by its weight, and summed on the log-odds scale from the base rate:

```
log-odds(score) = log-odds(base rate) + sum of weight x (x - x at the neutral value)
score = 1 / (1 + exp(-log-odds))
```

Every result carries `drivers`: one line per parameter with the value read, where it was read from, its contribution and
its direction, largest first. The contributions and the base rate's log-odds add up to the score, which a test checks
(`tests/test_agent_quality.py`). The configuration is one file, `config/voyagerai/propensity.yaml`: parameters, ranges,
weights, minimums and bands are edited there, not in code.

## Calibration: your outcomes, your model

The default weights are priors chosen from how these parameters usually behave. They rank passengers sensibly; they are
not fitted to any airline, so until you calibrate, read a score as a ranked risk and not as a measured probability.

`calibrate` takes your labelled history: one row per passenger with the parameters as columns and the outcome in the
column the prediction names (`churned` for churn). It needs at least 300 rows and 40 of each outcome. It then:

1. sets aside a quarter of the rows by a hash of the identifier (the same rows every time);
2. fits a logistic regression with a ridge penalty on the rest;
3. reports on the held-out quarter: AUC, Brier score, log loss, lift in the top tenth, and predicted against observed by
   fifth, next to the default scorecard's AUC and Brier on the same rows;
4. marks the fit `usable` only when the holdout AUC is at least 0.62;
5. with `save: true`, and only if usable, stores it as your model. Later `score` calls on your key use it and say
   `model.source: calibrated`. `reset: true` returns to the default; `model: default` scores one call with the default.

A saved model belongs to the customer whose key saved it (`data/voyagerai/propensity/<customer>/<objective>.json`). No
other customer's calls read it. On the bundled 2,400-row sample, whose outcomes were drawn from known weights
(`scripts/build_voyagerai_samples.py`), calibration recovers those weights and reaches a holdout AUC of 0.69 against 0.67
for the default, with a Brier score of 0.092 against 0.143.

## What it does not do

- It does not establish cause. A driver is an association in the scorecard; removing it is not promised to remove the risk.
- It scores passengers one at a time. It does not forecast totals, revenue or segment sizes.
- It does not invent a parameter from prose, from a tier name or from another parameter.
- Uncalibrated, its probabilities are not measured rates for your airline.
- It holds nothing of a passenger beyond the run's own record; a calibrated model stores weights, never rows.

## Try it

```bash
# what can be predicted, and from what
caf run vai_predictive_propensity --set action=describe
# one rich synthetic profile; a file of 40; a cancelled flight's passengers
caf run vai_predictive_propensity --set sample=elena_chen
caf run vai_predictive_propensity --set sample=passengers
caf run vai_predictive_propensity --set sample=disruption --set objective=disruption_sensitivity
# what a file of yours supports, before scoring it
caf run vai_predictive_propensity --set action=check --set profile=@my_passengers.csv
# fit to labelled outcomes (add --set save=true to keep the model)
caf run vai_predictive_propensity --set action=calibrate --set sample=churn_outcomes
```

The samples are in `sample_data/voyagerai/` and are synthetic.
