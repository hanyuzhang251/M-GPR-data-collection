#!/usr/bin/env python3
"""GPR frequency baseline — Caldara-Iacoviello methodology on a Kalshi corpus.

Replicates the CI (2022) construction as closely as the corpus allows:
counts the SHARE of documents in each period containing >=1 term from a
category word list, split into Threats and Acts, rescaled to a base period.

Two deliberate departures from CI, both forced by the data:

  1. UNIT OF OBSERVATION IS THE EVENT, NOT THE CONTRACT.
     The catalog holds ~11.3M contracts across ~458K events -- ~25 strikes
     per event. Strike count is wildly uneven across categories: a BTC price
     event lists dozens of strikes, a "will there be a ceasefire" event lists
     one. Counting contracts would underweight geopolitics by roughly the
     ratio of those strike counts. An event is the analogue of an article.

  2. TIME AXIS IS EXPIRY, NOT LISTING DATE.
     collect.py discards the API's open_time/created_time, so the only
     available time axis is expiry_date. "Contracts resolving in month t" is
     NOT CI's "articles published in month t". This is a known defect of the
     baseline, not a design choice -- see README note.

Lexicons: LITERAL_CI reproduces CI's newspaper-prose phrasing. ADAPTED
rewrites the same categories into the register prediction-market titles
actually use (interrogative, entity-led). Running both quantifies how much
of the measurement problem is register mismatch.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --------------------------------------------------------------------------
# Word lists.  Grouped into CI's eight categories; THREATS = 1-5, ACTS = 6-8.
# --------------------------------------------------------------------------

LITERAL_CI = {
    "war_threat": [
        r"risk of war", r"threat of war", r"war risk", r"fear of war",
        r"war fears?", r"military threat", r"prospect of war", r"danger of war",
    ],
    "peace_threat": [
        r"peace talks?", r"peace process", r"armistice", r"truce talks?",
    ],
    "military_buildup": [
        r"military buildups?", r"arms race", r"military mobili[sz]ation",
        r"troop buildups?", r"war games?", r"rearmament", r"military exercises?",
    ],
    "nuclear_threat": [
        r"nuclear war", r"nuclear weapons?", r"nuclear test", r"atomic weapons?",
        r"nuclear threat", r"nuclear proliferation", r"nuclear conflict",
    ],
    "terror_threat": [
        r"terrorist threat", r"terror threat", r"terrorism risk",
        r"threat of terrorism", r"terrorist plot",
    ],
    "war_begin": [
        r"outbreak of war", r"declaration of war", r"war broke out",
        r"began the war", r"start of the war",
    ],
    "war_escalate": [
        r"escalation of (the )?war", r"military escalation", r"air ?strikes?",
        r"aerial bombardment", r"military offensive",
    ],
    "terror_act": [
        r"terrorist attacks?", r"terror attacks?", r"suicide bombings?",
        r"hijacking", r"terrorist bombing",
    ],
}

ADAPTED = {
    "war_threat": [
        r"\bwar\b", r"\bconflict\b", r"\bhostilit", r"\binvade\b", r"\binvasion\b",
        r"\bmilitary action\b", r"\barmed conflict\b", r"\battack\b",
    ],
    "peace_threat": [
        r"\bceasefire\b", r"\bcease-fire\b", r"\bpeace (deal|agreement|talks|plan|treaty)\b",
        r"\btruce\b", r"\barmistice\b", r"\bpeace summit\b", r"\bnegotiat",
    ],
    "military_buildup": [
        r"\btroops?\b", r"\bmilitary\b", r"\bdeploy", r"\bmobili[sz]", r"\barms\b",
        r"\bwarship", r"\bnaval\b", r"\bair ?space\b", r"\bborder\b",
    ],
    "nuclear_threat": [
        r"\bnuclear\b", r"\batomic\b", r"\benrich(ed|ment)? uranium\b",
        r"\bICBM\b", r"\bballistic missile\b", r"\bwarhead",
    ],
    "terror_threat": [
        r"\bterror", r"\bhostage", r"\bassassinat", r"\bcoup\b", r"\binsurgen",
    ],
    "war_begin": [
        r"\bdeclare war\b", r"\bgo to war\b", r"\binvade\b", r"\binvasion\b",
        r"\boccupy\b", r"\bannex", r"\bseize\b",
    ],
    "war_escalate": [
        r"\bstrike\b", r"\bstrikes\b", r"\bbomb", r"\bmissile", r"\bdrone\b",
        r"\bescalat", r"\boffensive\b", r"\bshoot down\b", r"\bcasualt",
    ],
    "terror_act": [
        r"\bterrorist attack\b", r"\bbombing\b", r"\bshooting\b", r"\bhijack",
    ],
}

THREAT_CATS = ["war_threat", "peace_threat", "military_buildup",
               "nuclear_threat", "terror_threat"]
ACT_CATS = ["war_begin", "war_escalate", "terror_act"]


def compile_lexicon(lex):
    return {c: [re.compile(p, re.IGNORECASE) for p in pats if isinstance(p, str)]
            for c, pats in lex.items()}


def load_events(db_path):
    """One row per event: (expiry_month, name).  Event = CI's 'article'."""
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    cur = conn.execute(
        "SELECT event_ticker, MIN(expiry_date), MIN(name) "
        "FROM contracts GROUP BY event_ticker"
    )
    out = []
    for _et, expiry, name in cur:
        if not expiry or len(expiry) < 7:
            continue
        name = (name or "").replace("\n", " ").replace("\r", " ")
        out.append((expiry[:7], " ".join(name.split())))
    conn.close()
    return out


def build_index(events, lex, base_start, base_end, min_docs=200):
    """Share of events per month matching each bucket, rescaled base=100."""
    compiled = compile_lexicon(lex)
    threat_re = [r for c in THREAT_CATS for r in compiled.get(c, [])]
    act_re = [r for c in ACT_CATS for r in compiled.get(c, [])]

    tot = defaultdict(int)
    hits = defaultdict(lambda: defaultdict(int))
    cat_hits = defaultdict(int)

    for month, name in events:
        tot[month] += 1
        t = any(r.search(name) for r in threat_re)
        a = any(r.search(name) for r in act_re)
        if t:
            hits["threats"][month] += 1
        if a:
            hits["acts"][month] += 1
        if t or a:
            hits["gpr"][month] += 1
        for c, rs in compiled.items():
            if any(r.search(name) for r in rs):
                cat_hits[c] += 1

    months = sorted(m for m in tot if tot[m] >= min_docs)
    series = {}
    for bucket in ("gpr", "threats", "acts"):
        raw = {m: 100.0 * hits[bucket][m] / tot[m] for m in months}
        base = [raw[m] for m in months if base_start <= m <= base_end]
        scale = (sum(base) / len(base)) if base else 0.0
        series[bucket] = {
            "raw_pct": raw,
            "index": {m: (100.0 * raw[m] / scale if scale else float("nan"))
                      for m in months},
        }
    return months, tot, series, cat_hits


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/enriched_kalshi_catalog.db")
    ap.add_argument("--lexicon", choices=["literal", "adapted", "both"],
                    default="both")
    ap.add_argument("--base-start", default="2024-04")
    ap.add_argument("--base-end", default="2024-12")
    ap.add_argument("--min-docs", type=int, default=200,
                    help="drop months with fewer events (truncated bins)")
    ap.add_argument("--out", default="analysis/gpr_baseline.json")
    args = ap.parse_args()

    print(f"Loading events from {args.db} ...", file=sys.stderr)
    events = load_events(args.db)
    print(f"  {len(events):,} events", file=sys.stderr)

    results = {}
    lexicons = {"literal": LITERAL_CI, "adapted": ADAPTED}
    todo = lexicons.keys() if args.lexicon == "both" else [args.lexicon]

    for key in todo:
        months, tot, series, cat_hits = build_index(
            events, lexicons[key], args.base_start, args.base_end, args.min_docs)
        results[key] = {
            "months": months,
            "n_events": {m: tot[m] for m in months},
            "series": series,
            "category_hits": dict(cat_hits),
        }

        print(f"\n{'='*74}\n{key.upper()} LEXICON\n{'='*74}")
        print(f"{'month':<9}{'events':>9}{'GPR%':>9}{'Thr%':>8}{'Act%':>8}"
              f"{'GPR idx':>10}{'Thr idx':>10}{'Act idx':>10}")
        for m in months:
            g = series["gpr"]; t = series["threats"]; a = series["acts"]
            print(f"{m:<9}{tot[m]:>9,}{g['raw_pct'][m]:>9.2f}"
                  f"{t['raw_pct'][m]:>8.2f}{a['raw_pct'][m]:>8.2f}"
                  f"{g['index'][m]:>10.1f}{t['index'][m]:>10.1f}"
                  f"{a['index'][m]:>10.1f}")
        print(f"\n  category hit counts (events, whole sample):")
        for c in THREAT_CATS + ACT_CATS:
            tag = "T" if c in THREAT_CATS else "A"
            print(f"    [{tag}] {c:<18}{cat_hits.get(c,0):>9,}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
