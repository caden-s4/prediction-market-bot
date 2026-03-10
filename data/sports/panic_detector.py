"""
data.sports.panic_detector – detects order book panic / liquidity dislocation
in Kalshi sports markets.

A panic signal fires when a large order has temporarily crashed or spiked
the market price in a direction NOT justified by the current game state.
The edge is mean-reversion: fade the panic, expect the price to recover.

Detection logic (all must be true):
  1. price_gap = |book_implied_prob - model_prob| > 0.12
  2. depth_imbalance > 0.75  (one side owns 75%+ of visible book depth)
  3. model_prob NOT in [0.40, 0.60]  (avoid genuine uncertainty)
  4. price move is AGAINST the model
       book_prob < model_prob → market crashed YES, model says home winning
       book_prob > model_prob → market spiked YES, model says home losing

Trade direction: always FADE the panic.
  YES crashed → buy YES
  YES spiked  → buy NO  (equivalent to selling YES)

Position sizing: 0.5× base size only.  Panics can be informed — size
conservatively until history confirms they revert.

Confidence tiers:
  0.87  price_gap > 0.20 and depth_imbalance > 0.85
  0.81  price_gap > 0.12 and depth_imbalance > 0.75

Logging (every evaluation):
  PanicDetector: {market} | book_prob={X:.2f} model_prob={X:.2f}
      gap={X:.2f} imbalance={X:.2f} | panic={True/False}

Callers are responsible for fetching the order book ONLY for markets that
have already been matched to an active game — never fetch for unmatched markets.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from data.markets.base import OrderBook

logger = logging.getLogger(__name__)

# Thresholds
_PRICE_GAP_MIN = 0.12          # minimum gap between book and model
_PRICE_GAP_HIGH = 0.20         # high-confidence tier
_DEPTH_IMBALANCE_MIN = 0.75    # one side has this fraction of depth
_DEPTH_IMBALANCE_HIGH = 0.85   # high-confidence tier
_MODEL_UNCERTAIN_BAND = (0.40, 0.60)
_NEAR_PRICE_WINDOW = 0.05      # 5¢ — "near the best ask/bid"

# Confidence levels
_CONF_HIGH = 0.87
_CONF_LOW = 0.81
_CONF_TRADE_MIN = 0.81

# Panic position size multiplier — always trade at half size
PANIC_SIZE_MULTIPLIER = 0.5

# Per-cycle timing accumulator (reset by pipeline.py each cycle)
_cycle_elapsed_ms: float = 0.0


@dataclass
class PanicSignal:
    game_id: str
    market_id: str
    sport: str
    home_team: str
    away_team: str
    book_implied_prob: float     # market's current mid-price
    model_prob: float            # WinProbabilityModel output for the home team
    price_gap: float             # abs(book_implied_prob - model_prob)
    depth_imbalance: float       # max(yes_depth, no_depth) / (yes_depth + no_depth + 1)
    depth_yes: float             # contracts near best ask
    depth_no: float              # contracts near best bid
    trade_yes: bool              # True = buy YES, False = buy NO (fade panic)
    confidence: float
    size_multiplier: float = PANIC_SIZE_MULTIPLIER
    timestamp: float = field(default_factory=time.time)


def check_for_panic(
    market_id: str,
    order_book: OrderBook,
    model_prob: float,
    game_id: str,
    sport: str,
    home_team: str,
    away_team: str,
) -> Optional[PanicSignal]:
    """
    Evaluate an order book for panic / liquidity dislocation.

    Parameters
    ----------
    market_id   : Kalshi market ID (for logging)
    order_book  : fresh OrderBook snapshot — caller must not pass stale data
    model_prob  : current WinProbabilityModel output (home team probability)
    game_id     : ESPN game ID (for the signal record)
    sport       : "nfl" | "nba" | "ncaab"
    home_team   : for logging
    away_team   : for logging

    Returns
    -------
    PanicSignal if a panic is detected and confidence >= 0.81, else None.
    Always logs the evaluation.
    """
    global _cycle_elapsed_ms
    t0 = time.monotonic()

    result = _evaluate(market_id, order_book, model_prob, game_id, sport, home_team, away_team)

    elapsed = (time.monotonic() - t0) * 1000
    _cycle_elapsed_ms += elapsed
    return result


def _evaluate(
    market_id: str,
    order_book: OrderBook,
    model_prob: float,
    game_id: str,
    sport: str,
    home_team: str,
    away_team: str,
) -> Optional[PanicSignal]:
    best_ask = order_book.best_yes_ask
    best_bid = order_book.best_yes_bid

    if best_ask is None or best_bid is None:
        logger.debug("PanicDetector: %s has no ask or bid — skipping", market_id)
        return None

    book_implied_prob = (best_ask + best_bid) / 2.0
    price_gap = abs(book_implied_prob - model_prob)

    # Depth within 5¢ of best ask (YES depth) and best bid (NO proxy depth)
    ask_cutoff = best_ask + _NEAR_PRICE_WINDOW
    bid_cutoff = best_bid - _NEAR_PRICE_WINDOW

    depth_yes = sum(
        lvl.size for lvl in order_book.yes_asks if lvl.price <= ask_cutoff
    )
    depth_no = sum(
        lvl.size for lvl in order_book.yes_bids if lvl.price >= bid_cutoff
    )

    total_depth = depth_yes + depth_no
    # +1 in denominator avoids division by zero when book is completely empty
    depth_imbalance = max(depth_yes, depth_no) / (total_depth + 1)

    # Is the price move against the model?
    # book < model → market crashed YES → model says home winning, market disagreed → fade: buy YES
    # book > model → market spiked YES  → model says home losing  → fade: buy NO
    price_below_model = book_implied_prob < model_prob
    price_above_model = book_implied_prob > model_prob
    move_against_model = price_below_model or price_above_model

    model_uncertain = _MODEL_UNCERTAIN_BAND[0] <= model_prob <= _MODEL_UNCERTAIN_BAND[1]

    is_panic = (
        price_gap > _PRICE_GAP_MIN
        and depth_imbalance > _DEPTH_IMBALANCE_MIN
        and not model_uncertain
        and move_against_model
    )

    # Confidence
    if is_panic and price_gap > _PRICE_GAP_HIGH and depth_imbalance > _DEPTH_IMBALANCE_HIGH:
        confidence = _CONF_HIGH
    elif is_panic:
        confidence = _CONF_LOW
    else:
        confidence = 0.0

    logger.info(
        "PanicDetector: %s | book_prob=%.2f model_prob=%.2f gap=%.2f "
        "imbalance=%.2f depth_yes=%.0f depth_no=%.0f | panic=%s conf=%.2f",
        market_id,
        book_implied_prob, model_prob, price_gap,
        depth_imbalance, depth_yes, depth_no,
        is_panic, confidence,
    )

    if not is_panic or confidence < _CONF_TRADE_MIN:
        return None

    # Trade direction: always fade the panic
    trade_yes = price_below_model  # YES was hit down → buy YES to fade

    return PanicSignal(
        game_id=game_id,
        market_id=market_id,
        sport=sport,
        home_team=home_team,
        away_team=away_team,
        book_implied_prob=book_implied_prob,
        model_prob=model_prob,
        price_gap=price_gap,
        depth_imbalance=depth_imbalance,
        depth_yes=depth_yes,
        depth_no=depth_no,
        trade_yes=trade_yes,
        confidence=confidence,
    )


def reset_cycle_timing() -> float:
    """Return and reset the accumulated panic check time for this cycle."""
    global _cycle_elapsed_ms
    elapsed = _cycle_elapsed_ms
    _cycle_elapsed_ms = 0.0
    return elapsed
