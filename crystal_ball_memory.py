"""
crystal_ball_memory.py
======================
The self-improving learning engine for BTC Crystal Ball.

This module handles everything that makes the agent smarter over time:

  1. PREDICTION TRACKING   — Logs every prediction with full signal context
  2. OUTCOME EVALUATION    — After 7 and 30 days, checks if the prediction was right
  3. POST-MORTEM ANALYSIS  — Claude deeply analyzes each success and failure
  4. LESSONS LEARNED DB    — Synthesized, prioritized rules from all post-mortems
  5. INTELLIGENCE CONTEXT  — Feeds accumulated wisdom back into Claude before each prediction

The result: the agent reads its own history of hits and misses before every
prediction, steadily building a self-correcting "institutional memory."

Data files:
  crystal_ball_predictions.jsonl  — raw prediction log (one record per run)
  crystal_ball_lessons.json       — synthesized lessons & signal accuracy stats
  crystal_ball_postmortems.jsonl  — full post-mortem analyses
"""

import os, json, time, logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict
import requests
import anthropic

log = logging.getLogger("crystal_ball.memory")

PREDICTIONS_FILE  = "crystal_ball_predictions.jsonl"
LESSONS_FILE      = "crystal_ball_lessons.json"
POSTMORTEMS_FILE  = "crystal_ball_postmortems.jsonl"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL             = "claude-opus-4-6"

# ─── CORRECTNESS THRESHOLDS ───────────────────────────────────────────────────
# How we define whether a prediction was right.
# 7-day horizon is the primary evaluation window.
# 30-day horizon is the secondary (slower, stronger signal).

THRESHOLDS = {
    "STRONG_BUY":  {"7d": {"correct": 5.0,  "partial": 1.0},
                    "30d": {"correct": 12.0, "partial": 4.0}},
    "BUY":         {"7d": {"correct": 2.0,  "partial": 0.0},
                    "30d": {"correct": 8.0,  "partial": 2.0}},
    "HOLD":        {"7d": {"correct": 5.0,  "partial": 2.0},   # within ±5%
                    "30d": {"correct": 10.0, "partial": 5.0}},  # within ±10%
    "SELL":        {"7d": {"correct": -2.0, "partial": 0.0},
                    "30d": {"correct": -8.0, "partial": -2.0}},
    "STRONG_SELL": {"7d": {"correct": -5.0, "partial": -1.0},
                    "30d": {"correct": -12.0,"partial": -4.0}},
}


# ─── DATA STRUCTURES ──────────────────────────────────────────────────────────

@dataclass
class PredictionRecord:
    """
    One logged prediction cycle. Gets stored immediately.
    Outcome fields are filled in later by the evaluator.
    """
    id:               str          # YYYYMMDD_HHMM
    timestamp:        str          # ISO 8601
    signal:           str          # BUY, SELL, HOLD, etc.
    confidence:       int          # 0-100
    composite_score:  float
    price_at_pred:    float
    layman_headline:  str
    expert_reasoning: str

    # Full signal snapshot (for post-mortem attribution)
    signal_snapshot:  Dict         # {name: {value, data_quality, category}}
    macro_snapshot:   Dict         # {m2_yoy, fed_rate, dxy_yoy}
    fg_at_pred:       Optional[int]  # Fear & Greed value
    rsi_at_pred:      Optional[float]
    funding_at_pred:  Optional[float]
    rainbow_at_pred:  Optional[str]
    pi_gap_at_pred:   Optional[float]
    etf_flow_at_pred: Optional[float]
    hash_ribbon_at_pred: Optional[str]

    # Evaluation results (filled in later)
    evaluated_7d:     bool   = False
    outcome_7d:       Optional[str]  = None   # CORRECT / PARTIAL / INCORRECT
    price_7d:         Optional[float] = None
    pct_change_7d:    Optional[float] = None
    evaluated_30d:    bool   = False
    outcome_30d:      Optional[str]  = None
    price_30d:        Optional[float] = None
    pct_change_30d:   Optional[float] = None

    # Post-mortem
    postmortem_done:  bool   = False
    postmortem_id:    Optional[str] = None


@dataclass
class PostMortem:
    """
    Deep Claude analysis of a completed prediction.
    Stored separately and also synthesized into lessons_learned.
    """
    id:               str
    prediction_id:    str
    timestamp:        str
    signal_was:       str
    outcome_7d:       str
    price_move_7d:    float

    # Claude's analysis
    why_correct_or_wrong: str     # Root cause
    signals_that_helped:  List[str]
    signals_that_misled:  List[str]
    missed_context:       str     # What the agent didn't know or underweighted
    lessons_extracted:    List[str]  # Concrete actionable rules
    weight_suggestions:   Dict    # {"signal_name": {"direction": "increase/decrease", "reason": str}}
    confidence_calibration: str   # Was 74% confidence accurate? Should it have been higher/lower?


@dataclass
class LessonsLearned:
    """
    The living knowledge base — synthesized from all post-mortems.
    Loaded fresh before every Claude synthesis call.
    """
    last_updated:          str
    total_predictions:     int
    evaluated_predictions: int
    accuracy_7d:           float   # 0.0 - 1.0
    accuracy_30d:          float
    accuracy_7d_recent:    float   # last 20 predictions only

    # Per-signal accuracy (for weight calibration)
    signal_accuracy:       Dict    # {signal_name: {n, correct, accuracy, notes}}

    # Synthesized rules from post-mortems
    key_lessons:           List[str]    # ranked by how often they've been validated
    recent_errors:         List[Dict]   # last 5 mistakes with brief explanation
    recent_wins:           List[Dict]   # last 5 correct calls with what worked

    # Current model weaknesses (regenerated each synthesis)
    known_blind_spots:     List[str]

    # Context summary for Claude
    intelligence_summary:  str   # 3-5 sentence narrative Claude reads before predicting


# ─── PERSISTENCE HELPERS ──────────────────────────────────────────────────────

def _load_predictions() -> List[Dict]:
    """Load all prediction records from JSONL file."""
    records = []
    if not os.path.exists(PREDICTIONS_FILE):
        return records
    with open(PREDICTIONS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _save_prediction_record(record: Dict):
    """Append a prediction record to JSONL."""
    with open(PREDICTIONS_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _update_prediction_record(pred_id: str, updates: Dict):
    """Rewrite the JSONL file with updates applied to one record."""
    records = _load_predictions()
    updated = False
    for rec in records:
        if rec.get("id") == pred_id:
            rec.update(updates)
            updated = True
            break
    if updated:
        with open(PREDICTIONS_FILE, "w") as f:
            for rec in records:
                f.write(json.dumps(rec, default=str) + "\n")


def _save_postmortem(pm: Dict):
    """Append a post-mortem to the JSONL file."""
    with open(POSTMORTEMS_FILE, "a") as f:
        f.write(json.dumps(pm, default=str) + "\n")


def _load_postmortems() -> List[Dict]:
    """Load all post-mortems."""
    pms = []
    if not os.path.exists(POSTMORTEMS_FILE):
        return pms
    with open(POSTMORTEMS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    pms.append(json.loads(line))
                except:
                    pass
    return pms


def _load_lessons() -> Optional[Dict]:
    """Load the lessons database."""
    if not os.path.exists(LESSONS_FILE):
        return None
    try:
        with open(LESSONS_FILE) as f:
            return json.load(f)
    except:
        return None


def _save_lessons(lessons: Dict):
    """Save the lessons database."""
    with open(LESSONS_FILE, "w") as f:
        json.dump(lessons, f, indent=2, default=str)


# ─── PRICE FETCHER ────────────────────────────────────────────────────────────

def _get_coin_price_at(target_dt: datetime, coin_id: str = "bitcoin") -> Optional[float]:
    """
    Fetch historical price for any coin via CoinGecko.
    coin_id is the CoinGecko ID (e.g. "bitcoin", "ethereum", "solana").
    Used for outcome evaluation of swing trade predictions.
    Retries up to 3 times with exponential backoff on 429 rate limits.
    """
    date_str = target_dt.strftime("%d-%m-%Y")
    for attempt in range(3):
        try:
            r = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/history",
                params={"date": date_str, "localization": "false"},
                timeout=10
            )
            r.raise_for_status()
            data  = r.json()
            price = data.get("market_data", {}).get("current_price", {}).get("usd")
            time.sleep(2)   # always space calls even on success
            return float(price) if price else None
        except Exception as e:
            wait = 2 ** attempt * 3   # 3s, 6s, 12s
            log.warning(f"Historical price fetch failed for {coin_id} on {date_str} "
                        f"(attempt {attempt+1}/3): {e} — retrying in {wait}s")
            time.sleep(wait)
    return None

# Keep legacy alias for backwards compatibility
def _get_btc_price_at(target_dt: datetime) -> Optional[float]:
    return _get_coin_price_at(target_dt, "bitcoin")


# ─── MAIN PUBLIC API ──────────────────────────────────────────────────────────

def save_prediction(snap, analysis: Dict) -> str:
    """
    Called at the end of every run cycle.
    Logs the prediction with full context for later evaluation.
    Returns the prediction ID.
    """
    pred_id = datetime.now().strftime("%Y%m%d_%H%M")

    # Extract signal snapshot from snap.signals
    sig_snap = {}
    for s in getattr(snap, "signals", []):
        sig_snap[s.name] = {
            "value":        s.value,
            "data_quality": s.data_quality,
            "category":     s.category,
        }

    record = {
        "id":                 pred_id,
        "timestamp":          snap.timestamp,
        "signal":             analysis.get("signal", "HOLD"),
        "confidence":         analysis.get("confidence", 0),
        "composite_score":    analysis.get("composite_score", 0),
        # v3 swing trading: use selected coin price/id; fall back to BTC snap for v2
        "coin_id":            analysis.get("selected_coin_id",
                                  "bitcoin" if not analysis.get("selected_coin") else
                                  analysis.get("selected_coin", "bitcoin").lower()),
        "coin_ticker":        analysis.get("selected_coin",
                                  getattr(snap, "ticker", "BTC")),
        "price_at_pred":      analysis.get("selected_coin_price") or
                              (snap.price.get("usd", 0) if hasattr(snap, "price") else 0),
        "layman_headline":    analysis.get("layman_headline", ""),
        "expert_reasoning":   analysis.get("expert_reasoning", ""),
        "signal_snapshot":    sig_snap,
        "macro_snapshot": {
            "m2_yoy":   snap.macro.get("m2", {}).get("yoy_pct") if isinstance(snap.macro.get("m2"), dict) else None,
            "fed_rate": snap.macro.get("fed_rate", {}).get("value") if isinstance(snap.macro.get("fed_rate"), dict) else None,
            "dxy_yoy":  snap.macro.get("dxy", {}).get("yoy_pct") if isinstance(snap.macro.get("dxy"), dict) else None,
        },
        "fg_at_pred":         snap.fear_greed.get("current"),
        # v2-only fields — safe fallback for v3 MarketSnapshot
        "rsi_at_pred":        getattr(snap, "technicals", {}).get("rsi_14"),
        "funding_at_pred":    getattr(snap, "derivatives", {}).get("funding_rate_pct"),
        "rainbow_at_pred":    getattr(snap, "technicals", {}).get("rainbow_band"),
        "pi_gap_at_pred":     getattr(snap, "technicals", {}).get("pi_cycle_gap_pct"),
        "etf_flow_at_pred":   getattr(snap, "etf_flows", {}).get("latest_net_flow_m"),
        "hash_ribbon_at_pred":getattr(snap, "hash_ribbon", {}).get("status"),
        "evaluated_7d":       False,
        "outcome_7d":         None,
        "price_7d":           None,
        "pct_change_7d":      None,
        "evaluated_30d":      False,
        "outcome_30d":        None,
        "price_30d":          None,
        "pct_change_30d":     None,
        "postmortem_done":    False,
        "postmortem_id":      None,
    }

    _save_prediction_record(record)
    log.info(f"Prediction logged: [{pred_id}] {record['signal']} @ ${record['price_at_pred']:,.0f}")
    return pred_id


def evaluate_pending_predictions(current_price: float) -> List[Dict]:
    """
    Scans all unresolved predictions and evaluates those that have matured.
    - 7-day evaluation:  runs 7+ days after prediction
    - 30-day evaluation: runs 30+ days after prediction

    Returns list of newly evaluated records (for post-mortem triggering).
    """
    records    = _load_predictions()
    now        = datetime.now(timezone.utc)
    newly_eval = []

    for rec in records:
        ts = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00"))
        signal = rec.get("signal", "HOLD")
        price0 = rec.get("price_at_pred", 0)
        updated = False

        coin_id = rec.get("coin_id", "bitcoin")

        # 7-day evaluation
        if not rec.get("evaluated_7d") and (now - ts).days >= 7:
            price7 = _get_coin_price_at(ts + timedelta(days=7), coin_id)
            if price7 and price0:
                pct = round((price7 - price0) / price0 * 100, 2)
                outcome = _score_outcome(signal, pct, "7d")
                rec.update({
                    "evaluated_7d":   True,
                    "price_7d":       price7,
                    "pct_change_7d":  pct,
                    "outcome_7d":     outcome,
                })
                updated = True
                log.info(f"Evaluated [{rec['id']}]: {signal} → {outcome} (7d: {pct:+.1f}%)")

        # 30-day evaluation
        if not rec.get("evaluated_30d") and (now - ts).days >= 30:
            price30 = _get_coin_price_at(ts + timedelta(days=30), coin_id)
            if price30 and price0:
                pct = round((price30 - price0) / price0 * 100, 2)
                outcome = _score_outcome(signal, pct, "30d")
                rec.update({
                    "evaluated_30d":  True,
                    "price_30d":      price30,
                    "pct_change_30d": pct,
                    "outcome_30d":    outcome,
                })
                updated = True
                log.info(f"Evaluated [{rec['id']}]: {signal} → {outcome} (30d: {pct:+.1f}%)")

        # Flag for post-mortem if 7d complete and not yet analyzed
        if rec.get("evaluated_7d") and not rec.get("postmortem_done"):
            newly_eval.append(rec)

        if updated:
            _update_prediction_record(rec["id"], rec)

    return newly_eval


def _score_outcome(signal: str, pct_change: float, horizon: str) -> str:
    """Determine CORRECT / PARTIAL / INCORRECT for a signal and price change."""
    thresholds = THRESHOLDS.get(signal, THRESHOLDS["HOLD"])
    t          = thresholds.get(horizon, thresholds["7d"])

    if signal in ("BUY", "STRONG_BUY"):
        if pct_change >= t["correct"]: return "CORRECT"
        if pct_change >= t["partial"]: return "PARTIAL"
        return "INCORRECT"
    elif signal in ("SELL", "STRONG_SELL"):
        if pct_change <= t["correct"]: return "CORRECT"
        if pct_change <= t["partial"]: return "PARTIAL"
        return "INCORRECT"
    else:  # HOLD — correct if within band
        if abs(pct_change) <= t["correct"]: return "CORRECT"
        if abs(pct_change) <= t["partial"]: return "PARTIAL"
        return "INCORRECT"


def run_postmortem(record: Dict) -> Optional[Dict]:
    """
    Ask Claude to deeply analyze why a prediction succeeded or failed.
    Returns the post-mortem dict (also saved to file).
    """
    if not ANTHROPIC_API_KEY:
        log.warning("No ANTHROPIC_API_KEY — skipping post-mortem")
        return None

    client    = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    pred_id   = record["id"]
    signal    = record["signal"]
    outcome7  = record.get("outcome_7d", "UNKNOWN")
    pct7      = record.get("pct_change_7d", 0)
    price0    = record.get("price_at_pred", 0)
    price7    = record.get("price_7d", 0)

    # Load any existing lessons for context
    lessons = _load_lessons() or {}
    prior_lessons = lessons.get("key_lessons", [])

    system = """You are a forensic market analyst specializing in cryptocurrency prediction systems.
Your job is to conduct a rigorous post-mortem on a Bitcoin price prediction.

Be brutally honest. If the prediction was wrong, explain exactly why.
If it was correct, explain what signals worked and why, so we can increase their weight.

GOAL: Extract actionable rules that will make future predictions more accurate.

Good rules look like:
- "When Fed funds rate is rising AND M2 is contracting, BUY signals from technical indicators fail 80% of the time. Downweight RSI in this macro environment."
- "Fear & Greed below 20 combined with negative funding rate has been a reliable buy signal. Increase its weight."
- "Pi Cycle gap above 25% has no predictive value for short-term direction — only matters when gap < 10%."

Bad rules look like:
- "Be more careful next time"
- "Markets are unpredictable"

Respond ONLY with valid JSON."""

    prompt = f"""Conduct a post-mortem on this Bitcoin prediction.

PREDICTION MADE:
- Date: {record.get('timestamp', '')[:10]}
- Signal: {signal}
- Confidence: {record.get('confidence', 0)}%
- Price at prediction: ${price0:,.0f}
- Reasoning given: "{record.get('expert_reasoning', '')}"

OUTCOME (7 days later):
- Price: ${price7:,.0f} ({pct7:+.1f}%)
- Verdict: {outcome7}

SIGNAL VALUES AT TIME OF PREDICTION:
{json.dumps(record.get('signal_snapshot', {}), indent=2)}

MACRO AT TIME OF PREDICTION:
{json.dumps(record.get('macro_snapshot', {}), indent=2)}

Key individual values:
- Fear & Greed: {record.get('fg_at_pred')}
- RSI (14d): {record.get('rsi_at_pred')}
- Funding Rate: {record.get('funding_at_pred')}%
- Rainbow Band: {record.get('rainbow_at_pred')}
- Pi Cycle Gap: {record.get('pi_gap_at_pred')}%
- ETF Flow: ${record.get('etf_flow_at_pred')}M
- Hash Ribbon: {record.get('hash_ribbon_at_pred')}

PRIOR LESSONS (what we already know):
{json.dumps(prior_lessons[:10], indent=2)}

Respond with this JSON structure:
{{
  "verdict_summary": "One sentence: what happened and why the prediction was right or wrong",
  "root_cause": "The single most important factor that drove the outcome",
  "signals_that_helped": ["list of signal names that pointed correctly"],
  "signals_that_misled": ["list of signal names that pointed the wrong way"],
  "missed_context": "What information was absent or underweighted that would have changed the call",
  "lessons_extracted": [
    "Specific, actionable rule #1",
    "Specific, actionable rule #2",
    "Specific, actionable rule #3"
  ],
  "weight_suggestions": {{
    "SignalName": {{"direction": "increase | decrease | no_change", "reason": "why"}}
  }},
  "confidence_calibration": "Was {record.get('confidence')}% appropriate? Should it be higher/lower for these conditions?",
  "repeat_risk": "If same signals appeared again tomorrow, would this mistake repeat? Why/why not?"
}}"""

    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=2000, system=system,
            messages=[{"role":"user","content": prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        analysis = json.loads(raw.strip())

        pm = {
            "id":               f"pm_{pred_id}",
            "prediction_id":    pred_id,
            "timestamp":        datetime.now(timezone.utc).isoformat(),
            "signal_was":       signal,
            "confidence_was":   record.get("confidence", 0),
            "outcome_7d":       outcome7,
            "price_move_7d":    pct7,
            **analysis
        }
        _save_postmortem(pm)
        _update_prediction_record(pred_id, {"postmortem_done": True, "postmortem_id": pm["id"]})
        log.info(f"Post-mortem complete for [{pred_id}]: {outcome7}")
        return pm

    except Exception as e:
        log.error(f"Post-mortem failed for {pred_id}: {e}")
        return None


def rebuild_lessons_learned() -> Dict:
    """
    Synthesize ALL post-mortems into an updated lessons database.
    Called after each new post-mortem completes.
    """
    if not ANTHROPIC_API_KEY:
        return {}

    records   = _load_predictions()
    postmorts = _load_postmortems()
    client    = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Compute raw stats
    eval7  = [r for r in records if r.get("evaluated_7d")]
    eval30 = [r for r in records if r.get("evaluated_30d")]
    recent = eval7[-20:]  # last 20 evaluated predictions

    def accuracy(recs, horizon="7d"):
        outcomes = [r.get(f"outcome_{horizon}") for r in recs if r.get(f"outcome_{horizon}")]
        if not outcomes: return 0.0
        correct = sum(1 for o in outcomes if o == "CORRECT")
        partial = sum(1 for o in outcomes if o == "PARTIAL")
        return round((correct + partial * 0.5) / len(outcomes), 3)

    # Per-signal accuracy
    sig_stats = {}
    for r in eval7:
        snap = r.get("signal_snapshot", {})
        outcome = r.get("outcome_7d", "UNKNOWN")
        for sig_name, sig_data in snap.items():
            if sig_name not in sig_stats:
                sig_stats[sig_name] = {"n": 0, "correct": 0, "partial": 0, "incorrect": 0}
            sig_stats[sig_name]["n"] += 1
            # Was this signal pointing the right direction?
            sig_v    = sig_data.get("value", 0)
            signal   = r.get("signal", "HOLD")
            # Signal helped if it agreed with the overall signal AND outcome was correct
            direction_right = (
                (signal in ("BUY","STRONG_BUY") and sig_v > 0) or
                (signal in ("SELL","STRONG_SELL") and sig_v < 0) or
                (signal == "HOLD" and abs(sig_v) < 0.5)
            )
            if outcome == "CORRECT" and direction_right:
                sig_stats[sig_name]["correct"] += 1
            elif outcome == "PARTIAL":
                sig_stats[sig_name]["partial"] += 1
            elif outcome == "INCORRECT":
                sig_stats[sig_name]["incorrect"] += 1

    sig_accuracy = {}
    for name, stats in sig_stats.items():
        n = stats["n"]
        if n >= 3:  # only report if enough data
            acc = round((stats["correct"] + stats["partial"] * 0.5) / n, 3) if n else 0
            sig_accuracy[name] = {"n": n, "accuracy": acc, **stats}

    # Collect all extracted lessons from post-mortems
    all_lessons = []
    for pm in postmorts:
        all_lessons.extend(pm.get("lessons_extracted", []))

    # Recent errors and wins
    recent_sorted = sorted(eval7, key=lambda r: r.get("timestamp",""), reverse=True)[:10]
    errors = [r for r in recent_sorted if r.get("outcome_7d") == "INCORRECT"][:5]
    wins   = [r for r in recent_sorted if r.get("outcome_7d") == "CORRECT"][:5]

    recent_errors = [{"date": r["timestamp"][:10], "signal": r["signal"],
                      "price": r.get("price_at_pred"), "pct_move": r.get("pct_change_7d"),
                      "brief": next((pm.get("verdict_summary","") for pm in postmorts
                                     if pm.get("prediction_id") == r["id"]), "")}
                     for r in errors]

    recent_wins   = [{"date": r["timestamp"][:10], "signal": r["signal"],
                      "price": r.get("price_at_pred"), "pct_move": r.get("pct_change_7d"),
                      "brief": next((pm.get("verdict_summary","") for pm in postmorts
                                     if pm.get("prediction_id") == r["id"]), "")}
                     for r in wins]

    # Ask Claude to synthesize the lessons
    synthesis_prompt = f"""You are synthesizing {len(postmorts)} post-mortems from a Bitcoin prediction agent into a living lessons database.

ACCURACY STATS:
- Total predictions: {len(records)}
- Evaluated (7d): {len(eval7)}
- Accuracy (7d, all time): {accuracy(eval7, '7d') * 100:.1f}%
- Accuracy (7d, last 20): {accuracy(recent, '7d') * 100:.1f}%
- Accuracy (30d, all time): {accuracy(eval30, '30d') * 100:.1f}%

SIGNAL ACCURACY BREAKDOWN:
{json.dumps(sig_accuracy, indent=2)}

ALL RAW LESSONS FROM POST-MORTEMS ({len(all_lessons)} total):
{json.dumps(all_lessons, indent=2)}

RECENT ERRORS:
{json.dumps(recent_errors, indent=2)}

RECENT WINS:
{json.dumps(recent_wins, indent=2)}

Synthesize this into a living knowledge base. Consolidate duplicate lessons, rank by evidence strength, identify the primary blind spot.

Return valid JSON — every string must be written in plain English that a non-technical investor can understand (no jargon, no signal names as identifiers, explain what each signal actually measures):
{{
  "key_lessons": [
    "Most validated lesson — plain English, ranked by evidence strength",
    ...up to 15 lessons
  ],
  "top_3_patterns": [
    "Pattern 1 — the most consistently proven rule across all history, with context like 'correct X of Y times'",
    "Pattern 2",
    "Pattern 3"
  ],
  "red_flag": "The single pattern this model most consistently gets wrong. Be brutally honest. One plain-English sentence. Start with 'I consistently...' or 'My biggest weakness is...'",
  "most_recent_lesson": "The most important single lesson from the most recent post-mortem. Plain English. One sentence.",
  "known_blind_spots": [
    "Condition where the model consistently underperforms — plain English",
    ...up to 5
  ],
  "signal_notes": {{
    "SignalName": "One sentence on when this signal is reliable vs. unreliable"
  }},
  "intelligence_summary": "3-5 sentence narrative. Write in first person as if the crystal ball is speaking to its user. Plain English. No jargon.",
  "recommended_weight_changes": {{
    "SignalName": "increase | decrease | no_change — brief reason"
  }}
}}"""

    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=2000,
            messages=[{"role":"user","content": synthesis_prompt}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        synthesis = json.loads(raw.strip())
    except Exception as e:
        log.error(f"Lessons synthesis failed: {e}")
        synthesis = {"key_lessons": all_lessons[:10], "known_blind_spots": [],
                     "signal_notes": {}, "intelligence_summary": "",
                     "recommended_weight_changes": {}}

    # Collect individual (per-post-mortem) lessons for the UI — kept separate from collective synthesis
    individual_lessons_recent = []
    for pm in sorted(postmorts, key=lambda p: p.get("timestamp",""), reverse=True)[:10]:
        for lesson in pm.get("lessons_extracted", [])[:2]:
            individual_lessons_recent.append({
                "lesson":    lesson,
                "date":      pm.get("timestamp","")[:10],
                "outcome":   pm.get("outcome_7d",""),
                "signal":    pm.get("signal_was",""),
            })

    lessons = {
        "last_updated":               datetime.now(timezone.utc).isoformat(),
        "total_predictions":          len(records),
        "evaluated_predictions":      len(eval7),
        "accuracy_7d":                accuracy(eval7, "7d"),
        "accuracy_30d":               accuracy(eval30, "30d"),
        "accuracy_7d_recent":         accuracy(recent, "7d"),
        "signal_accuracy":            sig_accuracy,
        # Collective (synthesized across all history)
        "key_lessons":                synthesis.get("key_lessons", []),
        "top_3_patterns":             synthesis.get("top_3_patterns", []),
        "red_flag":                   synthesis.get("red_flag", ""),
        "known_blind_spots":          synthesis.get("known_blind_spots", []),
        "signal_notes":               synthesis.get("signal_notes", {}),
        "intelligence_summary":       synthesis.get("intelligence_summary", ""),
        "recommended_weight_changes": synthesis.get("recommended_weight_changes", {}),
        # Individual (per-post-mortem, not yet synthesized)
        "most_recent_lesson":         synthesis.get("most_recent_lesson", ""),
        "individual_lessons_recent":  individual_lessons_recent[:6],
        "recent_errors":              recent_errors,
        "recent_wins":                recent_wins,
    }

    _save_lessons(lessons)
    log.info(f"Lessons database rebuilt: {len(lessons['key_lessons'])} lessons, "
             f"7d accuracy: {lessons['accuracy_7d']*100:.1f}%")
    return lessons


def load_intelligence_context() -> str:
    """
    Returns a formatted string of accumulated wisdom that gets injected
    into Claude's system prompt BEFORE each prediction.
    This is the core of the self-improvement loop.
    """
    lessons = _load_lessons()
    if not lessons or lessons.get("total_predictions", 0) < 3:
        return ""  # Not enough history yet to be useful

    n_total  = lessons.get("total_predictions", 0)
    n_eval   = lessons.get("evaluated_predictions", 0)
    acc7     = lessons.get("accuracy_7d", 0)
    acc7r    = lessons.get("accuracy_7d_recent", 0)
    acc30    = lessons.get("accuracy_30d", 0)

    lines = [
        "=" * 60,
        "ACCUMULATED INTELLIGENCE (from your own prediction history)",
        "=" * 60,
        f"Total predictions made: {n_total}  |  Evaluated: {n_eval}",
        f"7-day accuracy (all time): {acc7*100:.1f}%",
        f"7-day accuracy (recent 20): {acc7r*100:.1f}%",
        f"30-day accuracy: {acc30*100:.1f}%",
        "",
    ]

    summary = lessons.get("intelligence_summary", "")
    if summary:
        lines += ["SELF-ASSESSMENT:", summary, ""]

    key_lessons = lessons.get("key_lessons", [])
    if key_lessons:
        lines.append("VALIDATED RULES (apply these to this prediction):")
        for i, lesson in enumerate(key_lessons[:12], 1):
            lines.append(f"  {i}. {lesson}")
        lines.append("")

    blind_spots = lessons.get("known_blind_spots", [])
    if blind_spots:
        lines.append("KNOWN BLIND SPOTS (be extra careful about these):")
        for spot in blind_spots[:4]:
            lines.append(f"  ⚠ {spot}")
        lines.append("")

    errors = lessons.get("recent_errors", [])
    if errors:
        lines.append("RECENT MISTAKES (don't repeat these):")
        for e in errors[:3]:
            lines.append(f"  ✗ {e.get('date','')} │ Called {e.get('signal','')} @ "
                         f"${e.get('price',0):,.0f} → price moved {e.get('pct_move',0):+.1f}%"
                         f"\n    → {e.get('brief','')}")
        lines.append("")

    sig_acc = lessons.get("signal_accuracy", {})
    if sig_acc:
        best  = sorted(sig_acc.items(), key=lambda x: x[1].get("accuracy",0), reverse=True)[:3]
        worst = sorted(sig_acc.items(), key=lambda x: x[1].get("accuracy",0))[:3]
        if best:
            lines.append("YOUR BEST SIGNALS: " + ', '.join('{} ({:.0f}%)'.format(k, v["accuracy"]*100) for k,v in best))
        if worst:
            lines.append("YOUR WORST SIGNALS: " + ', '.join('{} ({:.0f}%)'.format(k, v["accuracy"]*100) for k,v in worst))
        lines.append("")

    rec_wts = lessons.get("recommended_weight_changes", {})
    if rec_wts:
        lines.append("WEIGHT CALIBRATION (from post-mortem analysis):")
        for sig, note in list(rec_wts.items())[:8]:
            lines.append(f"  • {sig}: {note}")
        lines.append("")

    lines.append("=" * 60)
    lines.append("Use this intelligence to make a BETTER prediction than you would without it.")
    lines.append("=" * 60)

    return "\n".join(lines)


def get_performance_stats() -> Dict:
    """
    Returns summary statistics for display in the UI and HTML report.
    """
    records = _load_predictions()
    lessons = _load_lessons() or {}

    if not records:
        return {
            "total": 0, "evaluated": 0, "accuracy_7d": None,
            "accuracy_30d": None, "accuracy_recent": None,
            "streak": 0, "streak_type": None,
            "signal_accuracy": {}, "key_lessons": [],
        }

    eval7  = [r for r in records if r.get("evaluated_7d")]
    eval30 = [r for r in records if r.get("evaluated_30d")]

    # Current streak
    recent_outcomes = [r.get("outcome_7d") for r in sorted(
        eval7, key=lambda x: x.get("timestamp",""), reverse=True
    )[:20] if r.get("outcome_7d")]

    streak = 0
    streak_type = None
    if recent_outcomes:
        first = recent_outcomes[0]
        streak_type = "correct" if first in ("CORRECT","PARTIAL") else "incorrect"
        for o in recent_outcomes:
            if (streak_type == "correct" and o in ("CORRECT","PARTIAL")) or \
               (streak_type == "incorrect" and o == "INCORRECT"):
                streak += 1
            else:
                break

    def accuracy(recs, horizon="7d"):
        outcomes = [r.get(f"outcome_{horizon}") for r in recs if r.get(f"outcome_{horizon}")]
        if not outcomes: return None
        c = sum(1 for o in outcomes if o == "CORRECT")
        p = sum(1 for o in outcomes if o == "PARTIAL")
        return round((c + p * 0.5) / len(outcomes) * 100, 1)

    return {
        "total":                      len(records),
        "evaluated":                  len(eval7),
        "accuracy_7d":                accuracy(eval7, "7d"),
        "accuracy_30d":               accuracy(eval30, "30d"),
        "accuracy_recent":            accuracy(eval7[-20:], "7d") if len(eval7) >= 5 else None,
        "streak":                     streak,
        "streak_type":                streak_type,
        "signal_accuracy":            lessons.get("signal_accuracy", {}),
        # Three-tier learning log
        "most_recent_lesson":         lessons.get("most_recent_lesson", ""),
        "top_3_patterns":             lessons.get("top_3_patterns", [])[:3],
        "red_flag":                   lessons.get("red_flag", ""),
        # Separate individual vs collective
        "individual_lessons_recent":  lessons.get("individual_lessons_recent", []),
        "collective_lessons":         lessons.get("key_lessons", [])[:8],
        # Supporting context
        "recent_errors":              lessons.get("recent_errors", []),
        "recent_wins":                lessons.get("recent_wins", []),
        "intelligence_summary":       lessons.get("intelligence_summary", ""),
    }


def run_learning_cycle(current_price: float) -> Dict:
    """
    Master function — run at the end of each agent cycle.
    1. Evaluate any matured predictions
    2. Run post-mortems on newly resolved ones
    3. Rebuild lessons if new post-mortems were added

    Returns updated performance stats.
    """
    log.info("Running learning cycle...")

    # Wait for CoinGecko rate-limit window to recover after deep-dive OHLC calls
    time.sleep(30)

    # Evaluate predictions that have matured
    newly_resolved = evaluate_pending_predictions(current_price)

    # Run post-mortems on newly resolved predictions
    new_postmortems = 0
    for record in newly_resolved:
        pm = run_postmortem(record)
        if pm:
            new_postmortems += 1
        time.sleep(2)   # avoid API rate limits

    # Rebuild the lessons database if we have new post-mortems
    if new_postmortems > 0:
        log.info(f"Rebuilding lessons database after {new_postmortems} new post-mortems")
        rebuild_lessons_learned()

    return get_performance_stats()
