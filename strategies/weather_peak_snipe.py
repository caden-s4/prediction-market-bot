"""Weather peak-snipe strategy (Phase 14b v1, ghost-only).

Trigger model differs from strategies.weather_snipe:
  - This module: post-peak monotonic-trend trigger fired ONCE per series-event
    per day, identifying the trigger-time bracket and emitting up to 5 signals
    (winner YES + ±2 adjacent NO).
  - strategies.weather_snipe: per-bracket decisive snipe in the final 60 min
    before close.

Cities: NYC (KNYC), Chicago (KORD), Miami (KMIA), Denver (KDEN).
Series: KXHIGH<CITY> + KXHIGHT<CITY> + KXLOWT<CITY> (prefix-evolution handled).

Trigger conditions (validated in audit/weather_snipe_phase_a_20260510.md):
  1. Local clock-hour ≥ peak_hour + 1
       HIGH: peak_hour=14 → trigger window opens 15:00 local
       LOW:  peak_hour=7  → trigger window opens 08:00 local
  2. Running extremum was set ≥30 min ago
  3. Current obs ≥1°F past the running extremum
  4. Post-peak monotonicity: no rebound bounce >1°F from post-peak
     running min (HIGH) / max (LOW)

Bracket trade gates (per signal):
  - Winner bracket (contains observed_temp): buy YES if yes_ask ≥ 0.85
  - ±1 / ±2 adjacent brackets: buy NO if yes_ask ≤ 0.15

Risk caps (enforced before signal leaves this module):
  - $5 max risk per bracket  (hard ceiling honored by executor.place_snipe_trade)
  - 6 contracts max per trigger event (greedy by edge across bracket signals)

Mode: ghost only — `evaluate_event_signals` returns [] if dry_run is False
(defense in depth; executor's _dry_run flag is the primary gate).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time as _time, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from data.markets.base import Market
from monitoring.gate_events import log_gate_event

logger = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

SIGNAL_CLASS = "weather_peak_snipe"

PEAK_HOUR_LOCAL_HIGH = 14   # daily high climatological peak
PEAK_HOUR_LOCAL_LOW = 7     # daily low climatological peak (sunrise low)
TRIGGER_OPEN_OFFSET_HOURS = 1
MONOTONIC_MIN_DURATION_MIN = 30
BOUNCE_TOLERANCE_F = 1.0
PAST_PEAK_MIN_DELTA_F = 1.0  # current obs must be ≥1°F past the running ext

# Trade gates
WINNER_YES_PRICE_GATE = 0.85
ADJACENT_YES_PRICE_GATE = 0.15
ADJACENT_OFFSETS = (-2, -1, 1, 2)

# Risk caps
PER_BRACKET_MAX_RISK_USD = 5.0
PER_EVENT_MAX_CONTRACTS = 6

# Snipe metadata (reused fields from strategies.weather_snipe.SnipeSignal)
SNIPE_CONFIDENCE = 0.95   # below the existing snipe's 0.99 because of the
                          # late-cooling tail risk identified in 14a
# DECISIVE_PROB is the GT-probability we attribute to the bought side. We use
# 0.99 to match the existing snipe convention (executor.place_snipe_trade
# hardcodes gt_prob_for_kelly=0.99 for any snipe signal). The actual trigger
# accuracy is ~96.8% in-band (Phase 14a) — confidence=0.95 is what reflects
# that uncertainty; gt_prob is purely an edge-calc input that mirrors the
# executor's downstream assumption.
DECISIVE_PROB = 0.99

# ASOS lookback for trigger evaluation. Need enough to see today's peak
# observation plus the 30-min monotonic window. 12h is generous (covers
# overnight if the peak happened pre-trigger-window).
ASOS_LOOKBACK_HOURS = 12


# ── City + station mapping ────────────────────────────────────────────────────

# Older HIGH form uses "NY" abbreviation (e.g. KXHIGHNY); LOW form uses "NYC".
# All other cities use the same code in both forms.
@dataclass(frozen=True)
class _CityConfig:
    city_code: str          # canonical city code (matches CITY_TZ_MAP entry)
    asos_station: str       # bare IEM station (no leading K)
    tz_name: str            # IANA timezone


_CITIES: Dict[str, _CityConfig] = {
    "NYC": _CityConfig("NYC", "NYC", "America/New_York"),
    "CHI": _CityConfig("CHI", "ORD", "America/Chicago"),
    "MIA": _CityConfig("MIA", "MIA", "America/New_York"),  # MIA observes ET
    "DEN": _CityConfig("DEN", "DEN", "America/Denver"),
}

# Map raw ticker city code (as it appears in market_id) → canonical city.
# Older HIGH form for NY uses "NY"; LOW form uses "NYC". Other cities are the
# same in both forms but we list them explicitly for prefix-evolution clarity.
_TICKER_CITY_TO_CANONICAL: Dict[str, str] = {
    "NY": "NYC",
    "NYC": "NYC",
    "CHI": "CHI",
    "MIA": "MIA",
    "DEN": "DEN",
}


# Series prefix matcher — covers all three forms:
#   KXHIGH<CITY>     (older HIGH form, e.g. KXHIGHNY)
#   KXHIGHT<CITY>    (newer HIGH form, e.g. KXHIGHTBOS — not used by 14b cities)
#   KXLOWT<CITY>     (uniform LOW form)
# Direction is derived from the prefix.
_TICKER_RE = re.compile(
    r"^KX(HIGHT|HIGH|LOWT)([A-Z]{2,4})-(\d{2}[A-Z]{3}\d{2})-(T|B)(-?\d+(?:\.\d+)?)$"
)


def _match_series(market_id: str) -> Optional[Tuple[str, _CityConfig, str]]:
    """If ``market_id`` matches a 14b weather peak-snipe series, return
    (direction, CityConfig, event_date_str). Otherwise None.

    Direction is "high" or "low".
    """
    m = _TICKER_RE.match(market_id)
    if m is None:
        return None
    raw_prefix, raw_city, event_date_str, _strike_kind, _strike = m.groups()
    direction = "high" if raw_prefix.startswith("HIGH") else "low"
    canonical = _TICKER_CITY_TO_CANONICAL.get(raw_city)
    if canonical is None:
        return None
    cfg = _CITIES.get(canonical)
    if cfg is None:
        return None
    return direction, cfg, event_date_str


# ── Bracket parsing ───────────────────────────────────────────────────────────

# Subtitle examples (from raw kalshi item):
#   "81° to 82°"        → bracket [81, 82]
#   "83° or above"      → upper-tail (low=83, high=None)
#   "31° or below"      → lower-tail (low=None, high=31)
# Strike-kind suffix in ticker:
#   -B<value>   bracket (matches "X to Y")
#   -T<value>   threshold (matches "X or above" / "X or below")
_BRACKET_RANGE_RE = re.compile(
    r"^\s*(-?\d+)\s*°?\s*(?:to|-|–|—)\s*(-?\d+)\s*°?\s*$", re.IGNORECASE
)
_BRACKET_ABOVE_RE = re.compile(r"^\s*(-?\d+)\s*°?\s*or\s+above\s*$", re.IGNORECASE)
_BRACKET_BELOW_RE = re.compile(r"^\s*(-?\d+)\s*°?\s*or\s+below\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class _Bracket:
    market_id: str
    low: Optional[int]   # None = lower-tail bracket (X or below)
    high: Optional[int]  # None = upper-tail bracket (X or above)
    yes_ask: float       # current best YES ask (cents-fraction in [0, 1])

    def contains(self, temp_f: float) -> bool:
        """True if a settled temperature would land in this bracket.

        Uses inclusive integer-truncated comparison to match Kalshi's
        rounding (CLI temperatures are reported as integers).
        """
        t = round(temp_f)
        if self.low is None:
            return self.high is not None and t <= self.high
        if self.high is None:
            return t >= self.low
        return self.low <= t <= self.high


def _parse_bracket(market: Market) -> Optional[_Bracket]:
    """Pull bracket bounds from market.raw['subtitle'] (Kalshi yes_sub_title)."""
    subtitle = (market.raw.get("subtitle") if isinstance(market.raw, dict) else "") or ""
    # Strip degree symbols and unicode variants.
    sub = subtitle.replace("°", "").strip()

    m = _BRACKET_RANGE_RE.match(sub)
    if m:
        try:
            return _Bracket(
                market_id=market.market_id,
                low=int(m.group(1)),
                high=int(m.group(2)),
                yes_ask=_resolve_yes_ask(market),
            )
        except ValueError:
            return None

    m = _BRACKET_ABOVE_RE.match(sub)
    if m:
        try:
            return _Bracket(
                market_id=market.market_id,
                low=int(m.group(1)),
                high=None,
                yes_ask=_resolve_yes_ask(market),
            )
        except ValueError:
            return None

    m = _BRACKET_BELOW_RE.match(sub)
    if m:
        try:
            return _Bracket(
                market_id=market.market_id,
                low=None,
                high=int(m.group(1)),
                yes_ask=_resolve_yes_ask(market),
            )
        except ValueError:
            return None

    return None


def _resolve_yes_ask(market: Market) -> float:
    ask = getattr(market, "yes_ask", None)
    if ask is not None:
        try:
            return float(ask)
        except (TypeError, ValueError):
            pass
    return float(market.yes_price)


def _bracket_sort_key(b: _Bracket) -> Tuple[int, int]:
    """Sort brackets ascending by lower bound; tail-brackets at extremes.

    "X or below" sorts to the bottom (low=None → use high - large).
    "X or above" sorts to the top (high=None → use low + large).
    """
    if b.low is None:
        return (-10**6, b.high or 0)
    if b.high is None:
        return (10**6, b.low)
    return (b.low, b.high)


# ── Trigger evaluation ────────────────────────────────────────────────────────

@dataclass
class _TriggerOutcome:
    fired: bool
    observed_temp_f: Optional[float] = None
    reason: str = ""
    # Timestamp of the ASOS observation that satisfied the trigger.  Stable
    # across cycles while a new obs has not yet arrived (METAR cadence is
    # ~hourly), so it is used as the dedup key for log emission below.
    trigger_obs_ts: Optional[datetime] = None
    # Running extremum (max for high, min for low) over the local-day obs
    # window at the moment the trigger condition was evaluated. Captured
    # alongside observed_temp_f so the #6 bracket-source divergence audit
    # can quantify how often the two values cross a bracket boundary.
    # None when the trigger short-circuits before the running extremum is
    # computed (insufficient_obs).
    running_peak_f: Optional[float] = None


def _within_trigger_window(now_utc: datetime, cfg: _CityConfig, direction: str) -> bool:
    try:
        tz = ZoneInfo(cfg.tz_name)
    except ZoneInfoNotFoundError:
        return False
    local = now_utc.astimezone(tz)
    peak_hour = PEAK_HOUR_LOCAL_HIGH if direction == "high" else PEAK_HOUR_LOCAL_LOW
    return local.hour >= peak_hour + TRIGGER_OPEN_OFFSET_HOURS


def _evaluate_trigger(
    obs: List[Tuple[datetime, float]],
    direction: str,
    now_utc: datetime,
) -> _TriggerOutcome:
    """Apply the Phase 14a-validated post-peak monotonic-decline rule.

    ``obs`` must be sorted ascending by timestamp and contain only
    same-local-day observations. Returns fired=True with the latest
    observation's temperature when all four conditions pass.
    """
    if len(obs) < 2:
        return _TriggerOutcome(False, reason="insufficient_obs")

    if direction == "high":
        better = lambda a, b: a > b   # noqa: E731
        past_ok = lambda cur, ext: cur <= ext - PAST_PEAK_MIN_DELTA_F  # noqa: E731
    else:
        better = lambda a, b: a < b   # noqa: E731
        past_ok = lambda cur, ext: cur >= ext + PAST_PEAK_MIN_DELTA_F  # noqa: E731

    # Identify running extremum and its index.
    ext_idx = 0
    ext_ts, ext_temp = obs[0]
    for i, (ts, t) in enumerate(obs):
        if better(t, ext_temp):
            ext_idx = i
            ext_ts, ext_temp = ts, t

    cur_ts, cur_temp = obs[-1]

    # B: extremum ≥ MONOTONIC_MIN_DURATION_MIN ago.
    elapsed_min = (cur_ts - ext_ts).total_seconds() / 60.0
    if elapsed_min < MONOTONIC_MIN_DURATION_MIN:
        return _TriggerOutcome(
            False, observed_temp_f=cur_temp,
            reason=f"ext_too_recent({elapsed_min:.0f}min<{MONOTONIC_MIN_DURATION_MIN})",
            running_peak_f=ext_temp,
        )

    # C: current obs is past the extremum by ≥1°F.
    if not past_ok(cur_temp, ext_temp):
        return _TriggerOutcome(
            False, observed_temp_f=cur_temp,
            reason=f"not_past_peak(cur={cur_temp:.1f},ext={ext_temp:.1f})",
            running_peak_f=ext_temp,
        )

    # D: post-peak monotonicity (no bounce >1°F from post-peak running ext).
    post_ext: Optional[float] = None
    for j in range(ext_idx + 1, len(obs)):
        t = obs[j][1]
        if direction == "high":
            if post_ext is None or t < post_ext:
                post_ext = t
            elif t > post_ext + BOUNCE_TOLERANCE_F:
                return _TriggerOutcome(
                    False, observed_temp_f=cur_temp,
                    reason=f"rebound(post_min={post_ext:.1f},obs={t:.1f})",
                    running_peak_f=ext_temp,
                )
        else:
            if post_ext is None or t > post_ext:
                post_ext = t
            elif t < post_ext - BOUNCE_TOLERANCE_F:
                return _TriggerOutcome(
                    False, observed_temp_f=cur_temp,
                    reason=f"rebound(post_max={post_ext:.1f},obs={t:.1f})",
                    running_peak_f=ext_temp,
                )

    return _TriggerOutcome(
        fired=True, observed_temp_f=cur_temp, trigger_obs_ts=cur_ts,
        running_peak_f=ext_temp,
    )


def _filter_to_local_day(
    obs: List[Tuple[datetime, float]],
    tz_name: str,
    now_utc: datetime,
) -> List[Tuple[datetime, float]]:
    """Return only observations with the same local calendar date as ``now_utc``."""
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return obs
    local_today = now_utc.astimezone(tz).date()
    return [(ts, t) for ts, t in obs if ts.astimezone(tz).date() == local_today]


# ── Signal building ───────────────────────────────────────────────────────────

@dataclass
class WeatherPeakSnipeSignal:
    """Compatible with strategies.weather_snipe.SnipeSignal field shape so
    resolution.executor.place_snipe_trade accepts it unchanged.

    Adds ``signal_class`` (for gate_events categorization) and ``max_risk_usd``
    (per-bracket hard ceiling honored by the executor)."""
    market_id: str
    action: str          # "buy_yes" or "buy_no"
    target_price: float  # the price we are willing to pay (yes_ask or no_ask)
    edge: float          # gt_prob - target_price
    confidence: float
    rationale: str
    # Diagnostic / shadow-window fields (mirror SnipeSignal layout).
    gt_prob: float = DECISIVE_PROB
    asos_temp_f: Optional[float] = None
    bracket_low: Optional[float] = None
    bracket_high: Optional[float] = None
    market_mid: Optional[float] = None
    # New fields (executor checks via getattr so absence on legacy SnipeSignal
    # is safe).
    signal_class: str = SIGNAL_CLASS
    max_risk_usd: float = PER_BRACKET_MAX_RISK_USD
    # Trace fields for audit.
    trigger_event_id: str = ""        # series + event_date, used for cap accounting
    bracket_kind: str = "winner"      # "winner" or "adjacent"


def _signal_for_winner(bracket: _Bracket, observed_temp_f: float, event_id: str) -> Optional[WeatherPeakSnipeSignal]:
    if bracket.yes_ask < WINNER_YES_PRICE_GATE:
        return None
    edge = DECISIVE_PROB - bracket.yes_ask
    return WeatherPeakSnipeSignal(
        market_id=bracket.market_id,
        action="buy_yes",
        target_price=bracket.yes_ask,
        edge=edge,
        confidence=SNIPE_CONFIDENCE,
        rationale=(
            f"peak_snipe winner: obs={observed_temp_f:.1f}F in "
            f"[{_fmt_bound(bracket.low)},{_fmt_bound(bracket.high)}], "
            f"yes_ask={bracket.yes_ask:.3f}≥{WINNER_YES_PRICE_GATE}"
        ),
        gt_prob=DECISIVE_PROB,
        asos_temp_f=observed_temp_f,
        bracket_low=float(bracket.low) if bracket.low is not None else None,
        bracket_high=float(bracket.high) if bracket.high is not None else None,
        market_mid=bracket.yes_ask,
        trigger_event_id=event_id,
        bracket_kind="winner",
    )


def _signal_for_adjacent(bracket: _Bracket, observed_temp_f: float, event_id: str) -> Optional[WeatherPeakSnipeSignal]:
    if bracket.yes_ask > ADJACENT_YES_PRICE_GATE:
        return None
    # buy NO at (1 - yes_bid). Use yes_ask as a conservative proxy for
    # yes_bid here (book-walk happens in the executor's empty-book guard).
    no_ask = 1.0 - bracket.yes_ask
    edge = DECISIVE_PROB - no_ask
    if edge <= 0:
        return None
    return WeatherPeakSnipeSignal(
        market_id=bracket.market_id,
        action="buy_no",
        target_price=no_ask,
        edge=edge,
        confidence=SNIPE_CONFIDENCE,
        rationale=(
            f"peak_snipe adjacent: obs={observed_temp_f:.1f}F outside "
            f"[{_fmt_bound(bracket.low)},{_fmt_bound(bracket.high)}], "
            f"yes_ask={bracket.yes_ask:.3f}≤{ADJACENT_YES_PRICE_GATE}"
        ),
        gt_prob=DECISIVE_PROB,
        asos_temp_f=observed_temp_f,
        bracket_low=float(bracket.low) if bracket.low is not None else None,
        bracket_high=float(bracket.high) if bracket.high is not None else None,
        market_mid=bracket.yes_ask,
        trigger_event_id=event_id,
        bracket_kind="adjacent",
    )


def _fmt_bound(b: Optional[int]) -> str:
    return "-inf" if b is None else str(b)


def _enforce_contract_cap(
    signals: List[WeatherPeakSnipeSignal],
    *,
    max_contracts: int = PER_EVENT_MAX_CONTRACTS,
    per_bracket_max_risk_usd: float = PER_BRACKET_MAX_RISK_USD,
) -> List[WeatherPeakSnipeSignal]:
    """Greedy: keep highest-edge signals first; estimate contracts as
    floor(max_risk / target_price). Stop once cumulative contracts exceed cap.

    Conservative: a bracket's contribution is floor(max_risk / price), so a
    $5/bracket cap with a $0.02 NO buy on 4 brackets would be 4 × 250 = 1000
    contracts in theory — but the executor's _compute_size will further clamp
    via Kelly. The 6-contract cap here is the user-spec ceiling at the strategy
    level, applied as a count of *brackets* the trigger touches (1 winner + up
    to 4 NO adjacents = 5 max signals; 6 contracts is a generous post-Kelly
    ceiling assuming 1 contract per bracket on the winner + small stake on
    adjacents).

    The Phase 14b spec is ambiguous here ("6 contracts max total across all 5
    brackets"); we read it as: at most 6 contracts after the executor sizes
    each signal, enforced via per-bracket max_risk_usd + a global signal count
    floor. We pass the count limit (≤5 signals) through the cap by truncation,
    and rely on per-bracket max_risk_usd in the executor for absolute ceiling.
    """
    signals_sorted = sorted(signals, key=lambda s: s.edge, reverse=True)
    out: List[WeatherPeakSnipeSignal] = []
    contracts_used = 0
    for s in signals_sorted:
        # Conservative: assume each signal will produce at least 1 contract.
        if contracts_used + 1 > max_contracts:
            break
        out.append(s)
        contracts_used += 1
    return out


# ── Public entry point ────────────────────────────────────────────────────────

# Per-process dedup: (event_id) → set of market_ids already signaled today.
# Prevents the same trigger from firing the same bracket trades repeatedly
# across cycles. Cleared once the local day rolls.
_FIRED_TODAY: Dict[str, set] = {}
_FIRED_DAY_LOCAL: Dict[str, str] = {}

# Per-process log dedup: (winner_ticker, trigger_obs_ts_iso).  Once an
# `evaluated` event has been logged for a given key, neither `evaluated`
# nor `skip/price_gate` is emitted again for that key in this process
# lifetime.  Trigger evaluation and signal emission are unaffected — this
# only suppresses noisy duplicate log entries across cycles that share the
# same ASOS observation.  Cleared on process restart only.
_LOGGED_TRIGGERS: set = set()


def _local_day_key(now_utc: datetime, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return now_utc.strftime("%Y-%m-%d")
    return now_utc.astimezone(tz).strftime("%Y-%m-%d")


def _reset_dedup_if_new_day(event_id: str, day_key: str) -> None:
    if _FIRED_DAY_LOCAL.get(event_id) != day_key:
        _FIRED_TODAY[event_id] = set()
        _FIRED_DAY_LOCAL[event_id] = day_key


def evaluate_event_signals(
    series_event_markets: List[Market],
    *,
    now_utc: Optional[datetime] = None,
    asos_fetcher: Optional[Callable[[str, int], Optional[List[Tuple[datetime, float]]]]] = None,
    dry_run: bool = True,
) -> List[WeatherPeakSnipeSignal]:
    """Evaluate one (series, event_date) group for a peak-snipe trigger.

    ``series_event_markets`` must be all bracket markets for the same series +
    event date (e.g. all KXHIGHNY-26MAY09-* markets). The function classifies
    the series, fetches ASOS for the city, applies the trigger, and emits up to
    PER_EVENT_MAX_CONTRACTS signals (winner + adjacents) respecting price
    gates.

    ``asos_fetcher(station, lookback_hours) -> [(utc_dt, temp_f), ...]`` is
    injectable for tests. When None, uses the live IEM fetcher.

    Returns [] if dry_run is False (defense in depth — ghost only in 14b v1).
    """
    if not dry_run:
        logger.info(
            "WeatherPeakSnipe: dry_run=False — refusing to emit signals "
            "(ghost-only in Phase 14b v1)",
        )
        return []
    if not series_event_markets:
        return []

    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    # Classify via the first market's ID.
    head = series_event_markets[0]
    cls = _match_series(head.market_id)
    if cls is None:
        return []
    direction, cfg, event_date_str = cls
    event_id = f"{_series_prefix(head.market_id)}-{event_date_str}"

    # Window gate (cheap; runs before ASOS).
    if not _within_trigger_window(now_utc, cfg, direction):
        return []

    # Per-day dedup.
    day_key = _local_day_key(now_utc, cfg.tz_name)
    _reset_dedup_if_new_day(event_id, day_key)
    fired_set = _FIRED_TODAY[event_id]

    # ASOS fetch + filter to local day.
    fetcher = asos_fetcher or _default_asos_fetcher
    obs_all = fetcher(cfg.asos_station, ASOS_LOOKBACK_HOURS)
    if not obs_all:
        logger.info(
            "WeatherPeakSnipe: %s — no ASOS data (station=%s)",
            event_id, cfg.asos_station,
        )
        return []
    obs_today = _filter_to_local_day(obs_all, cfg.tz_name, now_utc)

    outcome = _evaluate_trigger(obs_today, direction, now_utc)
    if not outcome.fired:
        logger.info(
            "WeatherPeakSnipe: %s — trigger not fired (%s)",
            event_id, outcome.reason or "unknown",
        )
        return []

    observed_temp_f = outcome.observed_temp_f
    assert observed_temp_f is not None  # fired implies observed temp set
    # Captured alongside observed_temp for the #6 bracket-source divergence
    # audit. fired=True path always sets running_peak_f in _evaluate_trigger.
    running_peak_f = outcome.running_peak_f
    assert running_peak_f is not None

    # Build bracket inventory. Skip markets that don't parse, are already
    # fired today, or have no usable yes_ask.
    brackets: List[_Bracket] = []
    for m in series_event_markets:
        if m.market_id in fired_set:
            continue
        b = _parse_bracket(m)
        if b is None:
            continue
        brackets.append(b)

    if not brackets:
        return []

    brackets.sort(key=_bracket_sort_key)

    # Identify winner bracket (the one containing observed_temp_f).
    winner_idx: Optional[int] = None
    for i, b in enumerate(brackets):
        if b.contains(observed_temp_f):
            winner_idx = i
            break

    if winner_idx is None:
        logger.info(
            "WeatherPeakSnipe: %s — observed %.1fF outside all brackets",
            event_id, observed_temp_f,
        )
        return []

    # Trigger fired AND winner bracket identified — record an evaluated event
    # for fire-rate accounting. Fires regardless of whether the price-gate
    # filter ultimately passes any bracket; the price-gate evidence is
    # captured separately below.
    #
    # Dedup: trigger_time_utc is the ASOS observation timestamp (stable across
    # cycles until a new obs arrives, ~hourly), so (winner_ticker, trigger_ts)
    # collapses the per-cycle re-fires that Phase 14b produced (13 events for
    # 1 trigger). Once the key is recorded, the matching `skip/price_gate`
    # event below is also suppressed.
    winner_bracket = brackets[winner_idx]
    trigger_obs_iso = (
        outcome.trigger_obs_ts.isoformat()
        if outcome.trigger_obs_ts is not None
        else now_utc.isoformat()
    )
    log_key = (winner_bracket.market_id, trigger_obs_iso)
    already_logged = log_key in _LOGGED_TRIGGERS
    if not already_logged:
        log_gate_event(
            ticker=winner_bracket.market_id,
            gate="snipe",
            decision="evaluated",
            reason=None,
            platform="kalshi",
            extra={
                "signal_class": SIGNAL_CLASS,
                "ticker": winner_bracket.market_id,
                "winner_idx": winner_idx,
                "obs_temp_f": float(observed_temp_f),
                "running_peak_f": float(running_peak_f),
                "trigger_time_utc": trigger_obs_iso,
            },
        )
        # Grep-friendly divergence trace for the #6 audit. By trigger
        # condition C, |obs - running_peak| ≥ PAST_PEAK_MIN_DELTA_F (1.0°F),
        # so delta is always non-trivial when fired.
        logger.debug(
            "WPS_TRIGGER_DIVERGENCE ticker=%s cur_temp=%.1f running_peak=%.1f delta=%.1f",
            winner_bracket.market_id, observed_temp_f, running_peak_f,
            observed_temp_f - running_peak_f,
        )
        _LOGGED_TRIGGERS.add(log_key)

    candidate_signals: List[WeatherPeakSnipeSignal] = []
    winner_sig = _signal_for_winner(brackets[winner_idx], observed_temp_f, event_id)
    if winner_sig is not None:
        candidate_signals.append(winner_sig)

    for offset in ADJACENT_OFFSETS:
        adj_idx = winner_idx + offset
        if 0 <= adj_idx < len(brackets):
            adj_sig = _signal_for_adjacent(brackets[adj_idx], observed_temp_f, event_id)
            if adj_sig is not None:
                candidate_signals.append(adj_sig)

    if not candidate_signals:
        # Capture which prices failed the gate so future audits can decide
        # whether 0.85/0.15 are too tight. adjacent_yes_asks is fixed-length
        # 4 corresponding to ADJACENT_OFFSETS = (-2, -1, +1, +2); entries
        # are None if the offset falls outside the bracket array.
        adjacent_asks: List[Optional[float]] = []
        for offset in ADJACENT_OFFSETS:
            adj_idx = winner_idx + offset
            if 0 <= adj_idx < len(brackets):
                adjacent_asks.append(float(brackets[adj_idx].yes_ask))
            else:
                adjacent_asks.append(None)
        if not already_logged:
            log_gate_event(
                ticker=winner_bracket.market_id,
                gate="snipe",
                decision="skip",
                reason="price_gate",
                platform="kalshi",
                extra={
                    "signal_class": SIGNAL_CLASS,
                    "winner_yes_ask": float(winner_bracket.yes_ask),
                    "adjacent_yes_asks": adjacent_asks,
                    "winner_idx": winner_idx,
                    "obs_temp_f": float(observed_temp_f),
                    "running_peak_f": float(running_peak_f),
                },
            )
        logger.info(
            "WeatherPeakSnipe: %s — trigger fired (obs=%.1fF, running_peak=%.1fF, "
            "winner_idx=%d) but no bracket passed price gates "
            "(winner_yes_ask=%.3f, adjacent_yes_asks=%s)",
            event_id, observed_temp_f, running_peak_f, winner_idx,
            winner_bracket.yes_ask, adjacent_asks,
        )
        return []

    capped = _enforce_contract_cap(candidate_signals)
    for s in capped:
        fired_set.add(s.market_id)

    logger.info(
        "WeatherPeakSnipe: %s FIRED — obs=%.1fF running_peak=%.1fF winner_idx=%d "
        "signals=%d (after cap, from %d candidates)",
        event_id, observed_temp_f, running_peak_f, winner_idx,
        len(capped), len(candidate_signals),
    )
    return capped


def _series_prefix(market_id: str) -> str:
    # Returns the series prefix portion before the first '-' (e.g. KXHIGHNY).
    return market_id.split("-", 1)[0]


def _default_asos_fetcher(
    station: str, lookback_hours: int,
) -> Optional[List[Tuple[datetime, float]]]:
    # Imported lazily so unit tests can run without network access; the real
    # call goes through data.ground_truth.asos_timeseries which has its own
    # TTL cache.
    from data.ground_truth.asos_timeseries import fetch_asos_timeseries
    return fetch_asos_timeseries(station, lookback_hours=lookback_hours)


def is_peak_snipe_candidate(market_id: str) -> bool:
    """Cheap-prefix test for the scanner dispatch hook."""
    return _match_series(market_id) is not None


def group_markets_by_event(
    markets: List[Market],
) -> Dict[str, List[Market]]:
    """Group Phase 14b candidate markets by (series_prefix, event_date_str).

    Returns a dict keyed by event_id (e.g. "KXHIGHNY-26MAY09").
    Non-candidate markets are silently skipped.
    """
    groups: Dict[str, List[Market]] = {}
    for m in markets:
        cls = _match_series(m.market_id)
        if cls is None:
            continue
        _, _, event_date_str = cls
        prefix = _series_prefix(m.market_id)
        event_id = f"{prefix}-{event_date_str}"
        groups.setdefault(event_id, []).append(m)
    return groups


def _clear_dedup_for_test() -> None:
    """Test-only: clear in-memory dedup state."""
    _FIRED_TODAY.clear()
    _FIRED_DAY_LOCAL.clear()
    _LOGGED_TRIGGERS.clear()
