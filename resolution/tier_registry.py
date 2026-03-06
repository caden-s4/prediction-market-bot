"""
resolution.tier_registry – tiered market registry for the resolution drift bot.

Markets are ranked into three scan tiers based on time to resolution:

  Tier 1 — Active Watch   (<2h)   polled every ~15s; high-urgency candidates.
                                  Small set; worth the per-market API budget.

  Tier 2 — Regular Scan   (2–24h) polled every ~5min; pipeline markets.
                                  Gap signals here will still be present in 5min.

  Tier 3 — Discovery Scan (>24h)  polled every ~30min; awareness only.
                                  Looking for markets to graduate into Tier 2.

Promotion rules:
  - Markets only move towards Tier 1 (never demoted back to a higher number).
  - When hours_to_resolution crosses a boundary, the market is promoted.
  - A detected gap signal immediately forces any market to Tier 1 for the
    duration of the opportunity ("urgent" override), regardless of time left.
  - When the gap closes the urgent override is cleared; the market reverts to
    its natural tier.
  - Markets that have resolved (hours_to_resolution <= 0) are evicted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List

from data.markets.base import Market

logger = logging.getLogger(__name__)

# Tier boundary definitions
TIER_1_MAX_HOURS: float = 2.0    # < 2h → Tier 1 (active watch)
TIER_2_MAX_HOURS: float = 24.0   # 2–24h → Tier 2 (regular scan)
# > 24h → Tier 3 (discovery)


@dataclass
class MarketEntry:
    """A single market in the tier registry."""

    market: Market
    tier: int                         # 1, 2, or 3
    last_refreshed_at: float = field(default_factory=time.monotonic)
    signal_urgent: bool = False       # active gap signal → forced Tier 1
    sticky_t1: bool = False           # gap was detected at least once; keep T1 until
                                      # clear_sticky_t1() is called (survives signal_urgent
                                      # clearing so T1 persists across discovery rescans)

    @property
    def market_id(self) -> str:
        return self.market.market_id

    @property
    def platform(self) -> str:
        return self.market.platform


class TierRegistry:
    """
    Central registry of all known markets and their current scan tier.

    Lifecycle:
      ingest()        Add/update a market (assigns tier automatically).
      mark_urgent()   Force Tier 1 because a gap signal was detected.
      clear_urgent()  Remove urgent override; tier reverts to time-based.
      promote_due()   Sweep all entries and promote where time has crossed a boundary.
      evict_expired() Remove markets that have already resolved.
      remove()        Remove a specific market (position exited, etc.).

    Query:
      get_tier(n)     All MarketEntry objects in a given tier.
      all_markets()   Flat list of all Market objects in the registry.
      stats()         Dict with per-tier counts.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, MarketEntry] = {}   # market_id → entry

    # ── Tier assignment ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_tier(market: Market, urgent: bool = False) -> int:
        if urgent:
            return 1
        h = market.hours_to_resolution
        if h < 0:
            # Expired market — evict_expired() should have caught this; assign T1
            # so it surfaces immediately and gets evicted on the next sweep.
            logger.debug(
                "TierRegistry: _compute_tier %s has %.1fh < 0 — expired, assigning T1",
                market.market_id, h,
            )
            return 1
        tier = 1 if h <= TIER_1_MAX_HOURS else (2 if h <= TIER_2_MAX_HOURS else 3)
        logger.debug(
            "TierRegistry: tier assignment %s hours=%.1f → T%d",
            market.market_id, h, tier,
        )
        return tier

    # ── Ingestion ──────────────────────────────────────────────────────────────

    def ingest(self, market: Market) -> int:
        """
        Add or update a market.

        New markets start at the tier their time-to-resolution dictates.
        Existing entries keep their urgent flag; the tier is recomputed unless
        an urgent override is active.

        Returns the assigned tier.
        """
        entry = self._entries.get(market.market_id)
        if entry is None:
            tier = self._compute_tier(market)
            # Sanity check: time-based T1 assignment should only happen for
            # markets with < 2h remaining.  Anything else is a data bug.
            if tier == 1 and market.hours_to_resolution > TIER_1_MAX_HOURS:
                logger.error(
                    "TierRegistry: T1 misassignment — %s has %.1fh remaining "
                    "(expected < %.1fh for time-based T1). "
                    "Check hours_to_resolution calculation.",
                    market.market_id, market.hours_to_resolution, TIER_1_MAX_HOURS,
                )
            self._entries[market.market_id] = MarketEntry(market=market, tier=tier)
            logger.debug(
                "TierRegistry: new market %s → T%d (%.1fh left)",
                market.market_id, tier, market.hours_to_resolution,
            )
            return tier

        # Update market data (fresh yes_price, hours_to_resolution, etc.).
        entry.market = market
        entry.last_refreshed_at = time.monotonic()
        if not entry.signal_urgent:
            if entry.sticky_t1:
                # A gap signal was detected previously; keep T1 even though the
                # signal_urgent flag may have been cleared.  sticky_t1 survives
                # discovery rescans so recently-promoted markets aren't re-tiered
                # back to T2/T3 just because the gap briefly disappeared.
                entry.tier = 1
            else:
                new_tier = self._compute_tier(market)
                if new_tier < entry.tier:
                    logger.info(
                        "TierRegistry: %s T%d → T%d (%.1fh left, refresh-promoted)",
                        market.market_id, entry.tier, new_tier, market.hours_to_resolution,
                    )
                entry.tier = new_tier
        return entry.tier

    def ingest_many(self, markets: List[Market]) -> Dict[int, int]:
        """
        Bulk ingest.  Returns {tier: count} for the newly assigned tiers.
        Used after discovery scans to seed the registry.
        """
        counts: Dict[int, int] = {1: 0, 2: 0, 3: 0}
        for m in markets:
            t = self.ingest(m)
            counts[t] = counts.get(t, 0) + 1
        return counts

    # ── Urgent promotion ───────────────────────────────────────────────────────

    def mark_urgent(self, market_id: str) -> None:
        """Force a market to Tier 1 because an active gap signal was detected."""
        entry = self._entries.get(market_id)
        if entry:
            old_tier = entry.tier
            entry.signal_urgent = True
            entry.sticky_t1 = True   # persist T1 across discovery rescans
            entry.tier = 1
            if old_tier != 1:
                logger.info(
                    "TierRegistry: %s T%d → T1 (gap signal urgent, %.1fh remaining)",
                    market_id, old_tier, entry.market.hours_to_resolution,
                )

    def clear_urgent(self, market_id: str) -> None:
        """
        Remove the active gap-signal override.

        If sticky_t1=True the market stays in Tier 1 (it had a gap signal and
        may still be a near-term opportunity).  The tier only falls back to its
        natural time-based value when clear_sticky_t1() is also called — which
        should happen when the gap is confirmed closed or the position is entered.
        """
        entry = self._entries.get(market_id)
        if entry and entry.signal_urgent:
            entry.signal_urgent = False
            if entry.sticky_t1:
                # Keep T1 — gap may reappear; fast scan prevents missing it.
                logger.debug(
                    "TierRegistry: %s urgent cleared; staying T1 (sticky_t1=True)",
                    market_id,
                )
                return
            new_tier = self._compute_tier(entry.market)
            entry.tier = new_tier
            logger.debug(
                "TierRegistry: %s urgent cleared → T%d", market_id, new_tier
            )

    def clear_sticky_t1(self, market_id: str) -> None:
        """
        Clear the sticky T1 flag and revert to the natural time-based tier.

        Call this when the gap is confirmed closed (position entered, gap < 4%
        for multiple consecutive cycles, or the market is near resolution and
        should be evicted soon anyway).  The bot restart also clears sticky_t1
        implicitly since it lives only in memory.
        """
        entry = self._entries.get(market_id)
        if entry and entry.sticky_t1:
            entry.sticky_t1 = False
            if not entry.signal_urgent:
                new_tier = self._compute_tier(entry.market)
                old_tier = entry.tier
                entry.tier = new_tier
                if old_tier != new_tier:
                    logger.info(
                        "TierRegistry: %s sticky T1 cleared → T%d "
                        "(gap confirmed closed, %.1fh remaining)",
                        market_id, new_tier, entry.market.hours_to_resolution,
                    )

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def remove(self, market_id: str) -> None:
        """Remove a market (position exited, resolved, or manually removed)."""
        self._entries.pop(market_id, None)

    def promote_due(self) -> int:
        """
        Re-evaluate tier for all non-urgent entries and promote any that
        have crossed a tier boundary since the last evaluation.

        Markets only move towards Tier 1 (tier number decreasing); they are
        never demoted back to a higher tier number — a market in Tier 1 stays
        there until it resolves or is removed.

        Returns the number of markets promoted.
        """
        promoted = 0
        for entry in self._entries.values():
            if entry.signal_urgent:
                continue
            new_tier = self._compute_tier(entry.market)
            if new_tier < entry.tier:
                logger.info(
                    "TierRegistry: %s T%d → T%d (%.1fh left, time-promotion)",
                    entry.market_id, entry.tier, new_tier, entry.market.hours_to_resolution,
                )
                entry.tier = new_tier
                promoted += 1
        return promoted

    def evict_expired(self) -> int:
        """Remove markets with <= 0 hours remaining. Returns count removed."""
        expired = [
            mid for mid, e in self._entries.items()
            if e.market.hours_to_resolution <= 0
        ]
        for mid in expired:
            self._entries.pop(mid)
        if expired:
            logger.debug("TierRegistry: evicted %d expired market(s)", len(expired))
        return len(expired)

    # ── Queries ────────────────────────────────────────────────────────────────

    def get_tier(self, tier: int) -> List[MarketEntry]:
        """All entries in the given tier, sorted by market_id for stable ordering."""
        return sorted(
            [e for e in self._entries.values() if e.tier == tier],
            key=lambda e: e.market_id,
        )

    def all_markets(self) -> List[Market]:
        return [e.market for e in self._entries.values()]

    def known_ids(self) -> frozenset:
        return frozenset(self._entries.keys())

    def stats(self) -> Dict[str, int]:
        counts: Dict[str, int] = {"t1": 0, "t2": 0, "t3": 0, "total": 0}
        for e in self._entries.values():
            counts[f"t{e.tier}"] = counts.get(f"t{e.tier}", 0) + 1
        counts["total"] = len(self._entries)
        return counts

    def get_sticky_market_ids(self) -> List[str]:
        """Return market IDs where sticky_t1 is True (for persistence across restarts)."""
        return [mid for mid, e in self._entries.items() if e.sticky_t1]

    def restore_sticky_t1(self, market_ids) -> None:
        """Re-apply sticky_t1=True for markets present in the registry after a restart."""
        restored = 0
        for mid in market_ids:
            entry = self._entries.get(mid)
            if entry and not entry.sticky_t1:
                entry.sticky_t1 = True
                entry.tier = 1
                restored += 1
        if restored:
            logger.info("TierRegistry: restored %d sticky-T1 market(s)", restored)

    def __len__(self) -> int:
        return len(self._entries)
