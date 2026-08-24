#!/usr/bin/env python3
"""
kalshi_domains.py — Domain expertise layer for the Kalshi bet analyzer.

A generic forecaster loses to specialists. A sports bettor knows to check the
injury report before anything else; a political forecaster knows polls are
noisier than fundamentals 6 months out; a macro trader knows Fed funds futures
price the next cut better than any pundit.

This module classifies a question into a domain and returns:
  1. A FACTOR CHECKLIST — the specific things an expert in that field checks,
     in the order they check them, with the traps that fool amateurs.
  2. TARGETED SEARCH QUERIES — what to actually look up, so the analyst
     researches "Chiefs injury report" instead of vaguely googling the team.

The checklist is injected into the analyst's prompt so the model is forced to
address each factor explicitly rather than hand-waving.
"""

import re
from typing import Optional

# ─── DOMAIN DETECTION ─────────────────────────────────────────────────────────

# (weight, regex) — STRONG signals (weight 10) are unambiguous domain markers.
# WEAK signals (weight 1) are generic verbs that appear across many domains and
# must never decide a classification on their own.
_DOMAIN_PATTERNS = [
    ("sports_team", [
        (10, r"\b(nfl|nba|mlb|nhl|ncaa|mls|epl|premier league|ufc|f1|formula one|"
             r"super ?bowl|world series|stanley cup|march madness|world cup|"
             r"chiefs|lakers|yankees|celtics|dodgers|warriors|cowboys|eagles|packers|"
             r"bills|niners|49ers|heat|knicks|mets|braves|astros|rangers|bruins)\b"),
        (4,  r"\b(playoff|championship|finals|tournament|halftime|overtime|"
             r"starting lineup|injury report|home team|away team)\b"),
        (1,  r"\b(win|beat|defeat|cover|spread|game|match|season|series)\b"),
    ]),
    ("sports_player", [
        (10, r"\b(rushing yards|passing yards|touchdowns?|home runs?|strikeouts|"
             r"rebounds|assists|three.?pointers|receptions|player prop)\b"),
        (4,  r"\b(mvp|stat line|player of the|double.?double|hat trick)\b"),
        (1,  r"\b(points|goals|saves|yards)\b"),
    ]),
    ("politics_election", [
        (10, r"\b(election|elected|primary|caucus|nominee|nomination|ballot|"
             r"senate|governor|presidential|reelect|incumbent|electoral)\b"),
        (4,  r"\b(poll|polling|democrat|republican|gop|candidate|congress|house seat)\b"),
        (1,  r"\b(vote|votes|president)\b"),
    ]),
    ("politics_policy", [
        (10, r"\b(legislation|shutdown|impeach|executive order|veto|filibuster|"
             r"debt ceiling|appropriations|supreme court ruling)\b"),
        (4,  r"\b(bill|treaty|sanction|tariff|cabinet|confirmation hearing)\b"),
        (1,  r"\b(pass|passed|confirm|confirmed|nominate)\b"),
    ]),
    ("macro_econ", [
        (10, r"\b(fed|fomc|federal reserve|interest rate|rate cut|rate hike|"
             r"inflation|cpi|pce|gdp|nonfarm|payroll|jerome powell|central bank|ecb|"
             r"unemployment rate|jobs report|recession)\b"),
        (4,  r"\b(yield|treasury|basis points|bps|soft landing)\b"),
    ]),
    ("crypto", [
        (10, r"\b(bitcoin|btc|ethereum|eth|solana|xrp|ripple|dogecoin|doge|"
             r"litecoin|chainlink|cardano|crypto|altcoin|satoshi|stablecoin)\b"),
        (4,  r"\b(sol|ltc|link|ada|halving|on.?chain)\b"),
    ]),
    ("equity", [
        (10, r"\b(earnings|eps|ipo|nasdaq|s&p ?500|dow jones|market cap|"
             r"quarterly results|analyst estimate|stock price|shares outstanding)\b"),
        (10, r"\$[A-Z]{1,5}\b"),
        (4,  r"\b(stock|shares|revenue|guidance|dividend|acquisition|merger|buyback)\b"),
    ]),
    ("company_event", [
        (10, r"\b(fda approval|product launch|unveil|bankruptcy|layoffs?|"
             r"steps down|resigns?|acquisition close)\b"),
        (4,  r"\b(launch|release|announce|ceo|fired|recall|deadline)\b"),
        (1,  r"\b(product|ship|approval)\b"),
    ]),
    ("awards_culture", [
        (10, r"\b(oscar|academy award|grammy|emmy|golden globe|nobel|pulitzer|"
             r"best picture|best actor|best actress|box office|billboard|"
             r"rotten tomatoes|time person of the year)\b"),
        (4,  r"\b(album|movie|film|nominated|nomination for|opening weekend)\b"),
    ]),
    ("weather_climate", [
        (10, r"\b(hurricane|tornado|blizzard|snowfall|rainfall|el ni[nñ]o|la ni[nñ]a|"
             r"drought|wildfire|tropical storm|category [1-5])\b"),
        # Kalshi weather markets phrase it as "high temp"/"low temp" as often
        # as "temperature", and always name a city or degrees threshold.
        (4,  r"\b(temperature|temp|snow|storm|weather|climate|degrees|forecast)\b"),
        (4,  r"\b(high|low)\s+temp\b|\b\d{2,3}\s*°?\s*f\b|\bfahrenheit\b"),
    ]),
]

# A weak-only match (total score below this) is not trustworthy → fall back
_MIN_CONFIDENT_SCORE = 4


def detect_domain(text: str) -> str:
    """Classify a question into a domain. Returns a domain key or 'general'."""
    lower = text.lower()
    scores: dict[str, int] = {}
    for domain, patterns in _DOMAIN_PATTERNS:
        total = 0
        for weight, pattern in patterns:
            total += weight * len(re.findall(pattern, lower))
        if total:
            scores[domain] = total

    if not scores:
        return "general"

    best, best_score = max(scores.items(), key=lambda x: x[1])
    if best_score < _MIN_CONFIDENT_SCORE:
        return "general"
    return best


# ─── EXPERT CHECKLISTS ────────────────────────────────────────────────────────
# Each entry: what a genuine specialist checks, in priority order, plus the
# amateur traps. Written as instructions the model must answer explicitly.

_CHECKLISTS = {

"sports_team": """DOMAIN: TEAM SPORTS — you are a professional sports bettor, not a fan.

CHECK IN THIS ORDER (highest predictive value first):
1. INJURY REPORT — this is the single biggest edge in sports betting. Who is OUT, DOUBTFUL, QUESTIONABLE? Star player availability moves lines 3-10 points. A QB/point guard/goalie out is worth more than any trend. Check whether the market has already adjusted for known injuries — the edge is in news that broke in the last few hours.
2. RECORD AND FORM — overall W/L, but weight recent form (last 5-10 games) higher. Distinguish real quality from schedule luck.
3. STRENGTH OF SCHEDULE / QUALITY OF OPPONENT — a 7-2 record against bottom teams is worse than 5-4 against contenders.
4. HEAD-TO-HEAD HISTORY — matchup-specific: does one team's style consistently beat the other's?
5. HOME / AWAY SPLIT — home advantage is real but varies hugely by sport and team (~2.5 pts NFL, ~3 NBA, small in MLB).
6. REST AND TRAVEL — back-to-backs, short weeks, cross-country trips, bye weeks. Fatigue is measurable and underpriced.
7. MOTIVATION AND STAKES — is either team eliminated, resting starters, or playing for seeding? Late-season tanking is real.
8. WEATHER (outdoor only) — wind above 15mph destroys passing and kicking games; rain/snow lowers scoring.
9. COACHING AND SCHEME — specific matchup advantages, in-game management quality.
10. PUBLIC BETTING BIAS — popular teams are systematically overpriced. If the public loves a side, the price is inflated.

TRAPS THAT FOOL AMATEURS:
- Recency bias: one blowout does not change a team's true quality.
- Narrative over numbers: "they have momentum" is not evidence.
- Ignoring the fact that the market has ALREADY priced the obvious. The star being injured is priced in; the backup's actual quality often is not.
- Small sample head-to-head (3 games) is noise, not signal.

YOU MUST state the injury situation explicitly. If you could not find injury information, say so and lower your confidence — do not silently skip it.""",

"sports_player": """DOMAIN: PLAYER PROPS / INDIVIDUAL PERFORMANCE — you are a props specialist.

CHECK IN THIS ORDER:
1. IS THE PLAYER HEALTHY AND PLAYING? Injury status, minutes restriction, load management. A player who plays 20 minutes instead of 34 misses every over.
2. RECENT USAGE RATE — snap share, touches, targets, minutes, shot attempts over the last 3-5 games. Usage predicts production far better than talent.
3. OPPONENT DEFENSIVE RANK against that specific position/stat. Some defenses are elite overall but weak against one role.
4. TEAMMATE AVAILABILITY — if the other star is out, this player's usage spikes. This is the most exploitable prop edge that exists.
5. GAME SCRIPT — projected blowout means starters sit in the 4th; a close game means full minutes. Favored teams run more; trailing teams pass more.
6. PACE — fast-paced games create more possessions and more counting stats for everyone.
7. SEASON BASELINE vs the line — what's the player's actual per-game average and hit rate against this number?
8. HOME/AWAY AND REST splits for this specific player.

TRAPS:
- Season averages hide role changes. A player whose role changed 6 weeks ago has a misleading season average.
- Revenge-game and "he's due" narratives have zero predictive power.
- Ignoring blowout risk on an over.

YOU MUST state the player's health/usage status explicitly, or say you couldn't confirm it and lower confidence.""",

"politics_election": """DOMAIN: ELECTIONS — you forecast like Nate Silver, not like a partisan.

CHECK IN THIS ORDER:
1. POLLING AVERAGE, not single polls. Individual polls are noise. Check the aggregate, the trend direction, and the spread between pollsters.
2. POLL QUALITY AND HOUSE EFFECTS — pollster ratings, sample size, likely-voter vs registered-voter screens, partisan-sponsored polls lean toward their sponsor.
3. FUNDAMENTALS — incumbency (a large real advantage), economic conditions, presidential approval, generic ballot, district partisan lean (PVI). Far out from election day, fundamentals beat polls.
4. TIME TO ELECTION — polls 6+ months out have weak predictive power; polls in the final 2 weeks are strong. Weight accordingly.
5. HISTORICAL POLLING ERROR — polls have missed by 3-5 points in recent cycles, often systematically in one direction. Uncertainty bands must be wide.
6. TURNOUT MODEL — who actually shows up? Midterms, primaries, and specials have very different electorates.
7. CANDIDATE QUALITY AND SCANDALS — recent, specific, and material only.
8. MONEY AND ADS — fundraising as a proxy for enthusiasm, but weak on its own.
9. PRIMARY DYNAMICS — in multi-candidate fields, consolidation matters more than current standing.

TRAPS:
- Assuming the polling error will repeat in the same direction — it may reverse.
- Treating a 2-point lead as a lock; that's well inside the margin.
- Vibes, rally sizes, and yard signs are not data.
- Conflating national polls with the specific race being asked about.

YOU MUST cite the actual current polling numbers if the question is polling-driven, or state that you could not retrieve them.""",

"politics_policy": """DOMAIN: LEGISLATION / GOVERNMENT ACTION — you are a Capitol Hill analyst.

CHECK IN THIS ORDER:
1. STATUS QUO BIAS IS ENORMOUS. Most bills die. Most nominations that stall, fail. Default heavily toward "nothing happens" unless there is a scheduled, calendared action.
2. VOTE COUNT — do the votes actually exist? Chamber margins, filibuster threshold (60 in Senate), announced defections.
3. LEADERSHIP INTENT — has leadership scheduled floor time? Nothing passes without floor time. This is the single best predictor.
4. DEADLINE PRESSURE — funding deadlines, expiring authorizations, and debt ceilings force action that would otherwise never happen.
5. PROCEDURAL PATH — reconciliation vs regular order, committee stage, rules committee, discharge petitions.
6. PRESIDENTIAL POSITION — veto threat kills most things absent a supermajority.
7. HISTORICAL BASE RATE — roughly 3-5% of introduced bills become law. Confirmations of non-controversial nominees pass at high rates; controversial ones stall.
8. ELECTION PROXIMITY — legislating slows dramatically near elections.

TRAPS:
- Believing press statements over whip counts.
- Assuming "bipartisan support" in a press release means votes exist.
- Underestimating how often deadlines slip via short-term extensions.""",

"macro_econ": """DOMAIN: MACRO / CENTRAL BANKS — you are a rates strategist.

CHECK IN THIS ORDER:
1. MARKET-IMPLIED ODDS FIRST — Fed funds futures and the CME FedWatch tool price rate decisions better than any analyst. If the question is about a Fed move, that number IS the answer unless you have a strong reason otherwise.
2. OFFICIAL GUIDANCE — the dot plot, recent FOMC statement language, and speeches from voting members. The Fed telegraphs; surprises are rare.
3. THE DATA THAT DRIVES THE DECISION — CPI, PCE, core inflation trend, unemployment, nonfarm payrolls. Where are they versus target?
4. CONSENSUS FORECAST vs WHISPER NUMBER for data-release questions. The consensus is the base case; the distribution of forecaster estimates gives you the uncertainty band.
5. RELEASE CALENDAR — is the data print before or after the market closes? What exact series and revision does the market resolve on?
6. RECENT SURPRISE HISTORY — has this series been beating or missing consensus lately? Streaks in data surprises are mildly persistent.
7. BLACKOUT PERIODS — before FOMC meetings, officials go quiet; no new information will arrive.

TRAPS:
- Fighting the futures market with a narrative.
- Ignoring which specific index the market resolves on (headline vs core, seasonally adjusted vs not).
- Assuming one hot inflation print flips policy — the Fed moves on trends.""",

"crypto": """DOMAIN: CRYPTO PRICE — you are a quantitative derivatives trader.

CHECK IN THIS ORDER:
1. THE VOLATILITY MODEL IS YOUR ANCHOR. You are given a lognormal probability. For pure price-threshold questions, this is objective and usually beats intuition. Start there and adjust only for the factors below.
2. DISTANCE IN STANDARD DEVIATIONS, not percent. A 5% move is trivial over a week and near-impossible in 2 hours. Always reason in sigmas over the remaining window.
3. VOLATILITY REGIME — is realized vol elevated or compressed right now? Compressed vol means the model may understate tail moves (vol clusters and expands suddenly).
4. SCHEDULED CATALYSTS in the window — CPI prints, FOMC, ETF decisions, unlock events, halving, major listings. These break the drift-free assumption and fatten tails.
5. KEY TECHNICAL LEVELS — round numbers and prior highs/lows act as magnets and barriers, and are where liquidations cluster.
6. FUNDING RATES AND OPEN INTEREST — extreme positive funding means crowded longs and squeeze risk downward; the reverse for negative.
7. CORRELATION — crypto trades with risk assets. A big equity selloff drags it down regardless of crypto-specific news.
8. LIQUIDATION CASCADES — leveraged markets gap through levels; the lognormal model understates the odds of violent moves toward liquidation clusters.

TRAPS:
- Trusting chart patterns over the volatility math.
- Ignoring that the model assumes zero drift — it does not know about a scheduled catalyst.
- Treating a 24-hour move as a trend.""",

"equity": """DOMAIN: EQUITIES / EARNINGS — you are an equity analyst.

CHECK IN THIS ORDER:
1. IMPLIED MOVE FROM OPTIONS — the options straddle prices the expected earnings move. That's the market's real volatility estimate; use it like the crypto vol model.
2. CONSENSUS ESTIMATE vs WHISPER — for earnings-beat questions, what's the analyst consensus and how often does this company beat it? Most large caps beat consensus ~70% of the time by design.
3. RECENT GUIDANCE AND PRE-ANNOUNCEMENTS — company guidance is the strongest signal; a pre-announcement resolves most uncertainty.
4. HISTORICAL BEAT RATE AND REACTION — how has this specific stock moved on the last 8 earnings? Some stocks beat and still fall.
5. SECTOR AND PEER RESULTS — peers reporting first give a strong read on the quarter.
6. INSIDER ACTIVITY AND UNUSUAL OPTIONS FLOW — clusters of insider buying are mildly predictive; single sales are not.
7. SHORT INTEREST AND POSITIONING — high short interest creates squeeze asymmetry.
8. THE EXACT RESOLUTION SOURCE — GAAP vs adjusted EPS, which fiscal period, closing price vs intraday.

TRAPS:
- Confusing "beat earnings" with "stock goes up" — they diverge constantly.
- Ignoring that expectations, not absolute results, drive the move.""",

"company_event": """DOMAIN: CORPORATE / PRODUCT EVENTS — you forecast corporate behavior.

CHECK IN THIS ORDER:
1. STATUS QUO AND DELAY BIAS — announced timelines slip far more often than they hold. Default toward "later than promised."
2. OFFICIAL COMMITMENTS — has the company given a firm, dated, public commitment? Firm dates with regulatory or contractual consequences hold much better than aspirational ones.
3. TRACK RECORD OF THIS SPECIFIC COMPANY on hitting announced dates. Some chronically slip; some never do.
4. REGULATORY GATES — FDA, FCC, antitrust. Check the actual PDUFA date or review clock, and the historical approval rate for that class.
5. SUPPLY CHAIN AND PRODUCTION SIGNALS — manufacturing reports, hiring, filings, teardown leaks.
6. LEADERSHIP INCENTIVES — is there a quarter-end, bonus, or investor-day reason to ship by a date?
7. THE EXACT RESOLUTION CRITERIA — "launch" can mean announce, preorder, or ship. Read the rules carefully; ambiguity favors NO.

TRAPS:
- Believing CEO hype timelines.
- Missing that the market resolves on a narrower definition than the headline suggests.""",

"awards_culture": """DOMAIN: AWARDS / ENTERTAINMENT — you are an awards-season forecaster.

CHECK IN THIS ORDER:
1. PRECURSOR AWARDS ARE THE STRONGEST SIGNAL. Guild awards (PGA, DGA, SAG, WGA), Golden Globes, and critics' groups predict Oscars with high accuracy. The DGA winner almost always takes Best Director.
2. VOTING BODY COMPOSITION AND SYSTEM — Academy preferential ballot rewards broadly-liked consensus picks over polarizing favorites. Know who votes and how.
3. NOMINATION COUNT — total nominations signal breadth of support and correlate strongly with the top prize.
4. CAMPAIGN INTENSITY AND NARRATIVE — "overdue" narratives genuinely move voters. Studio campaign spend matters.
5. HISTORICAL PATTERNS AND BIASES — genre bias (drama over comedy/horror), recency of release, biopic advantage for acting categories.
6. BACKLASH RISK — frontrunners peaking too early get overtaken.
7. FOR BOX OFFICE: opening weekend tracking, presales, theater count, comps to similar titles.

TRAPS:
- Confusing critical acclaim with voter appeal.
- Twitter consensus is not voter consensus.""",

"weather_climate": """DOMAIN: WEATHER — you are a meteorologist reading model output.

CHECK IN THIS ORDER:
1. MODEL CONSENSUS — GFS, ECMWF (Euro), and ensemble spread. The Euro is generally the more accurate global model. Agreement across models means high confidence; divergence means genuine uncertainty.
2. FORECAST HORIZON — skill collapses fast. 1-3 days is reliable, 4-7 days is moderate, beyond 10 days is barely better than climatology.
3. CLIMATOLOGY AS BASE RATE — what's normal for this location and date? This is your prior when models disagree.
4. ENSEMBLE SPREAD — the distribution of ensemble members IS the probability. If 30 of 50 members cross the threshold, that's 60%.
5. EXACT MEASUREMENT STATION AND DEFINITION — official station location, the precise threshold, midnight-to-midnight vs 24-hour window. Weather markets are won on resolution details.
6. PERSISTENCE AND PATTERN — blocking patterns and established regimes persist longer than models often suggest.
7. SEASONAL DRIVERS — El Niño/La Niña state shifts the whole distribution.

TRAPS:
- Reading a single model run as truth.
- Ignoring the specific official measurement station named in the rules.""",

"general": """DOMAIN: GENERAL — no specialist checklist matched, so reason from first principles.

CHECK IN THIS ORDER:
1. REFERENCE CLASS — what category of event is this, and how often does that category resolve YES historically?
2. STATUS QUO — the current state of the world persisting is almost always the highest-probability outcome. Change requires a mechanism.
3. WHO CONTROLS THE OUTCOME — is this a decision by a known actor with known incentives, or a stochastic event?
4. TIME REMAINING vs the amount of change required.
5. IS THERE A SCHEDULED EVENT that forces resolution, or does this depend on something spontaneous happening?
6. CONJUNCTION CHECK — does this require multiple independent things to all happen? If so, multiply the probabilities down.
7. RESOLUTION AMBIGUITY — read the rules; ambiguous criteria usually resolve NO.

TRAPS:
- Substituting a vivid story for a base rate.
- Overweighting recent news.""",
}


# ─── TARGETED SEARCH QUERIES ──────────────────────────────────────────────────

_SEARCH_TEMPLATES = {
    "sports_team": [
        "{q} injury report today",
        "{q} latest odds and starting lineup",
        "{q} recent form last 5 games record",
    ],
    "sports_player": [
        "{q} injury status minutes",
        "{q} recent game log stats",
        "{q} matchup defense rank",
    ],
    "politics_election": [
        "{q} latest polls average",
        "{q} forecast model odds",
    ],
    "politics_policy": [
        "{q} vote count schedule latest news",
        "{q} floor vote timing",
    ],
    "macro_econ": [
        "{q} CME FedWatch market odds",
        "{q} latest data consensus forecast",
    ],
    "crypto": [
        "{q} price news today",
        "crypto market catalysts this week",
    ],
    "equity": [
        "{q} earnings consensus estimate",
        "{q} latest news analyst",
    ],
    "company_event": [
        "{q} latest announcement timeline",
        "{q} delay news",
    ],
    "awards_culture": [
        "{q} predictions frontrunner",
        "{q} precursor awards winners",
    ],
    "weather_climate": [
        "{q} forecast models",
        "{q} National Weather Service forecast",
    ],
    "general": [
        "{q} latest news",
    ],
}


DOMAIN_LABELS = {
    "sports_team":       "Team Sports",
    "sports_player":     "Player Props",
    "politics_election": "Elections",
    "politics_policy":   "Legislation / Government",
    "macro_econ":        "Macro / Central Banks",
    "crypto":            "Crypto Price",
    "equity":            "Equities / Earnings",
    "company_event":     "Corporate Events",
    "awards_culture":    "Awards / Entertainment",
    "weather_climate":   "Weather",
    "general":           "General",
}


def get_checklist(domain: str) -> str:
    return _CHECKLISTS.get(domain, _CHECKLISTS["general"])


def get_search_queries(domain: str, question: str) -> list[str]:
    """Concrete search strings the analyst should run for this domain."""
    core = re.sub(r"^(will|is|does|can|who|what|when)\s+", "", question.strip(), flags=re.I)
    core = core.rstrip("?").strip()
    templates = _SEARCH_TEMPLATES.get(domain, _SEARCH_TEMPLATES["general"])
    return [t.format(q=core) for t in templates]


def build_domain_block(question: str, market_title: str = "") -> dict:
    """
    Full domain package for the analyst prompt.
    Returns {domain, label, checklist, searches}
    """
    domain = detect_domain(f"{question} {market_title}")
    return {
        "domain":    domain,
        "label":     DOMAIN_LABELS.get(domain, "General"),
        "checklist": get_checklist(domain),
        "searches":  get_search_queries(domain, question),
    }


if __name__ == "__main__":
    tests = [
        "Will the Chiefs beat the Bills on Sunday?",
        "Will Patrick Mahomes throw for over 300 yards?",
        "Will the Fed cut rates in December?",
        "Will Bitcoin be above $120,000 this week?",
        "Will the Democrats win the Senate?",
        "Will NVDA beat earnings estimates?",
        "Will Oppenheimer win Best Picture?",
        "Will there be a hurricane landfall in Florida in September?",
        "Will Congress pass the budget bill?",
        "Will Apple release the new iPhone before October?",
        "Will aliens be confirmed to exist?",
    ]
    for t in tests:
        d = build_domain_block(t)
        print(f"{d['label']:28s} <- {t}")
        for s in d["searches"]:
            print(f"      search: {s}")
        print()
