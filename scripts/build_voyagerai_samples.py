"""Builds the VoyagerAI sample data under sample_data/voyagerai/. Everything is synthetic: the passengers are invented, the
airline is fictional, and the labelled outcomes are drawn from a known formula (printed below) so that calibration has a truth
to be checked against. Deterministic: the same script writes the same files.

    python3 scripts/build_voyagerai_samples.py
"""

from __future__ import annotations

import csv
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "sample_data" / "voyagerai"
AS_OF = date(2026, 11, 20)
TIERS = ["Member", "Silver", "Gold", "Platinum Elite"]
FIRST = ["Elena", "James", "Aiko", "Mateo", "Priya", "Noah", "Zainab", "Lucas", "Mei", "Omar", "Sofia", "Kwame", "Ingrid", "Tomas", "Leila", "Arjun",
         "Hana", "Diego", "Amara", "Felix"]
LAST = ["Chen", "Okafor", "Tanaka", "Alvarez", "Raman", "Bergstrom", "Hussein", "Moreau", "Lin", "Farouk", "Rossi", "Mensah", "Larsen", "Novak", "Haddad", "Iyer"]

# The truth the labelled outcomes are drawn from (log-odds; x is each parameter on the scorecard's own range). It differs on
# purpose from the default scorecard's priors: a calibration that works recovers this, not the priors.
TRUTH = {"intercept": -1.25, "disruptions_28d": 1.1, "competitor_browsing_28d": 2.0, "booking_trend": -1.8, "days_since_last_booking": 1.5, "complaints_90d": 0.6,
         "nps_last": -1.2, "app_uninstall_intent": 0.4, "redemption_declines_90d": 0.3, "app_sessions_30d": -0.9, "tenure_years": -0.2, "tier_level": -0.1}
RANGES = {"disruptions_28d": (0, 4), "competitor_browsing_28d": (0, 3), "booking_trend": (0, 1.5), "days_since_last_booking": (0, 180), "complaints_90d": (0, 3),
          "nps_last": (0, 10), "app_uninstall_intent": (0, 1), "redemption_declines_90d": (0, 3), "app_sessions_30d": (0, 12), "tenure_years": (0, 10), "tier_level": (0, 3)}


def x(name: str, value: float) -> float:
    lo, hi = RANGES[name]
    return min(1.0, max(0.0, (value - lo) / (hi - lo)))


def person(rng: random.Random, i: int) -> dict:
    tier = rng.choices(range(4), weights=[40, 28, 20, 12])[0]
    prev = rng.randint(1, 6 + tier * 5)
    now = max(0, round(prev * rng.uniform(0.3, 1.5)))
    return {"passenger_id": f"P-{100000 + i}", "tier": TIERS[tier], "tenure_years": round(rng.uniform(0.2, 12), 1), "bookings_12m": now, "bookings_prev_12m": prev,
            "days_since_last_booking": rng.choice([3, 9, 14, 21, 30, 45, 60, 75, 90, 120, 150, 210]), "disruptions_28d": rng.choices(range(5), weights=[55, 22, 12, 7, 4])[0],
            "competitor_browsing_28d": rng.choices(range(4), weights=[62, 20, 11, 7])[0], "redemption_declines_90d": rng.choices(range(4), weights=[70, 18, 8, 4])[0],
            "complaints_90d": rng.choices(range(4), weights=[78, 14, 6, 2])[0], "nps_last": rng.choices(range(11), weights=[2, 2, 3, 3, 4, 6, 9, 14, 20, 20, 17])[0],
            "app_uninstall_intent": rng.random() < 0.08, "app_sessions_30d": rng.choices(range(13), weights=[14, 10, 10, 9, 9, 8, 8, 7, 7, 6, 5, 4, 3])[0], "_tier": tier}


def churn_probability(p: dict) -> float:
    z = TRUTH["intercept"]
    values = {**p, "booking_trend": p["bookings_12m"] / p["bookings_prev_12m"], "tier_level": p["_tier"], "app_uninstall_intent": float(p["app_uninstall_intent"])}
    for name, w in TRUTH.items():
        if name != "intercept":
            z += w * x(name, float(values[name]))
    return 1 / (1 + math.exp(-z))


def outcomes() -> None:
    rng = random.Random(20261120)
    cols = ["passenger_id", "tier", "tenure_years", "bookings_12m", "bookings_prev_12m", "days_since_last_booking", "disruptions_28d", "competitor_browsing_28d",
            "redemption_declines_90d", "complaints_90d", "nps_last", "app_uninstall_intent", "app_sessions_30d", "churned"]
    with open(OUT / "churn_outcomes_sample.csv", "w", newline="\n", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for i in range(2400):
            p = person(rng, i)
            churned = rng.random() < churn_probability(p)
            row = {**p, "app_uninstall_intent": str(p["app_uninstall_intent"]).lower(), "churned": int(churned)}
            for hole in ("nps_last", "app_sessions_30d", "tenure_years"):   # real exports have gaps
                if rng.random() < 0.06:
                    row[hole] = ""
            w.writerow([row[c] for c in cols])


def passengers() -> None:
    rng = random.Random(7)
    out = []
    for i in range(40):
        p = person(rng, 500 + i)
        tier = p.pop("_tier")
        rec = {"passenger_id": p["passenger_id"], "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}", "loyalty_id": f"XA{rng.randint(100000, 999999)}", "hub": rng.choice(["SIN", "YYC", "LHR", "DXB", "SYD"]),
               "ltv_usd": rng.choice([1800, 4200, 9600, 15400, 27800, 52400]) * (1 + tier), **p,
               "cabin": rng.choice(["Economy", "Economy", "Premium Economy", "Business"]), "flight_hours": rng.choice([1.5, 2.5, 4, 7, 11, 13]),
               "upgrades_12m": rng.choices(range(4), weights=[60, 25, 10, 5])[0], "ancillary_spend_12m_usd": rng.choice([0, 40, 120, 260, 540, 980]),
               "miles_balance": rng.choice([800, 6400, 18200, 41000, 96500, 196800])}
        if i % 9 == 4:   # a thin record: too little to score, to show that it is refused and listed, never guessed
            rec = {"passenger_id": rec["passenger_id"], "name": rec["name"], "tier": rec["tier"], "hub": rec["hub"]}
        out.append(rec)
    (OUT / "passengers_sample.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")


def elena() -> None:
    d = lambda days: (AS_OF - timedelta(days=days)).isoformat()   # noqa: E731
    rec = {
        "passenger_id": "P-204117", "name": "Elena Chen", "loyalty_id": "XA198800", "pnr": "K7RQ2M", "tier": "Platinum Elite", "hub": "SIN", "member_since": "2017-03-14",
        "ltv_usd": 52400, "miles_balance": 196800, "bookings_12m": 14, "bookings_prev_12m": 21, "last_booking_date": d(52), "nps_last": 5, "segments": ["platinum-elite", "long-haul-business"],
        "cabin": "Business", "flight_hours": 13.5, "upgrades_12m": 2, "ancillary_spend_12m_usd": 640, "travel_purpose": "business", "as_of": AS_OF.isoformat(), "history_complete": True,
        "history": [
            {"date": d(26), "type": "disruption", "source": "PSS", "detail": "SIN-LHR delayed 3h10, missed onward connection", "severity": "high"},
            {"date": d(17), "type": "disruption", "source": "PSS", "detail": "LHR-SIN cancelled, rebooked next day", "severity": "critical"},
            {"date": d(6), "type": "disruption", "source": "PSS", "detail": "SIN-HKG delayed 1h45", "severity": "medium"},
            {"date": d(5), "type": "competitor_browsing", "source": "Web analytics", "detail": "Viewed a competitor's SIN-DXB fare page", "severity": "critical"},
            {"date": d(4), "type": "competitor_browsing", "source": "Web analytics", "detail": "Price comparison on a metasearch for SIN-LHR", "severity": "high"},
            {"date": d(12), "type": "redemption_decline", "source": "Loyalty engine", "detail": "Award seat SIN-LHR unavailable", "severity": "medium"},
            {"date": d(3), "type": "redemption_decline", "source": "Loyalty engine", "detail": "Upgrade with miles declined", "severity": "medium"},
            {"date": d(9), "type": "complaint", "source": "Contact centre", "detail": "Complaint about the missed connection", "severity": "high"},
            {"date": d(2), "type": "app_uninstall_intent", "source": "App telemetry", "detail": "Notification permission revoked, uninstall flow opened", "severity": "high"},
            {"date": d(21), "type": "app_session", "source": "App telemetry", "detail": "App opened"},
            {"date": d(8), "type": "app_session", "source": "App telemetry", "detail": "App opened"},
        ]}
    (OUT / "passenger_elena_chen.json").write_text(json.dumps(rec, indent=1) + "\n", encoding="utf-8")


def disruption() -> None:
    rng = random.Random(217)
    out = []
    for i in range(14):
        tier = rng.choices(range(4), weights=[40, 25, 20, 15])[0]
        party = rng.choices([1, 2, 3, 4], weights=[55, 25, 12, 8])[0]
        out.append({"passenger_id": f"P-{300100 + i}", "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}", "pnr": "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)),
                    "tier": TIERS[tier], "flight": "XA217", "is_cancelled": True, "delay_minutes": 0, "missed_connection": rng.random() < 0.35, "overnight_required": rng.random() < 0.5,
                    "rebook_options": rng.choice([0, 1, 2, 3, 5]), "party_size": party, "special_assistance": party > 2 and rng.random() < 0.5,
                    "travel_purpose": rng.choice(["business", "leisure", "leisure"]), "disruptions_28d": rng.choices(range(4), weights=[60, 25, 10, 5])[0]})
    (OUT / "disruption_manifest_sample.json").write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    outcomes()
    passengers()
    elena()
    disruption()
    for f in sorted(OUT.glob("*")):
        print(f"{f.name:<36} {f.stat().st_size:>8,} bytes")
