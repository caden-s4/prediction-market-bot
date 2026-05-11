"""monitoring/gate_names.py — Stable string constants for gate IDs and reason codes.

Import these in Phase B/C/D wire-up rather than using freeform strings.
"""

# ── Gate identifiers ──────────────────────────────────────────────────────────

GATE_SCANNER_REJECT   = "scanner_reject"
GATE_GT_ROUTING       = "gt_routing"
GATE_CONFIDENCE       = "confidence"
GATE_EXECUTOR_PRETRADE = "executor_pretrade"
GATE_SNIPE            = "snipe"

# ── Reason codes — namespaced by gate to avoid collisions ─────────────────────

# Scanner reject
REASON_FINANCIAL_BRACKET_DISABLED   = "financial_bracket_disabled"
REASON_LEGACY_WEATHER_SNIPE_DISABLED = "legacy_weather_snipe_disabled"
REASON_EXCLUDED                     = "excluded"
REASON_CATEGORY                     = "category"
REASON_HOURS                        = "hours"
REASON_PRICE                        = "price"

# Confidence
REASON_SOURCE_BELOW_GATE          = "source_below_gate"
REASON_FRESHNESS_BELOW_GATE       = "freshness_below_gate"
REASON_CLARITY_BELOW_GATE         = "clarity_below_gate"
REASON_BOTH_BELOW_GATE            = "both_below_gate"
REASON_DIRECTION_AMBIGUOUS        = "direction_ambiguous"

# GT routing
REASON_NO_SOURCE_MATCHED          = "no_source_matched"
REASON_SOURCE_NOT_TRADEABLE       = "source_not_tradeable"
REASON_SOURCE_RETURNED_NONE       = "source_returned_none"

# Executor pre-trade
REASON_GT_STALE_AT_ENTRY                  = "gt_stale_at_entry"
REASON_LARGE_DIVERGENCE_EXTREME           = "large_divergence_extreme_market"
REASON_BANKROLL                           = "bankroll"
REASON_DEDUP                              = "dedup"
REASON_SERIES_CAP                         = "series_cap"
REASON_EMPTY_BOOK_GHOST                   = "empty_book_ghost"
REASON_EMPTY_BOOK_SNIPE                   = "empty_book_snipe"
REASON_PERM_SKIP_CONFIDENCE_FAILURES      = "perm_skip_confidence_failures"

# Snipe
REASON_NO_SIGNAL                  = "no_signal"
REASON_ALREADY_PRICED             = "already_priced_no_edge"
REASON_ASOS_FETCH_FAILED          = "asos_fetch_failed"
