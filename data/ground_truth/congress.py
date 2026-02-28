"""
data.ground_truth.congress – US Congress bill status via Congress.gov API.

Source: api.congress.gov/v3  (free, no API key required for public use)

Covers:
  - Bill passage (signed into law, vetoed, passed a chamber)
  - Congressional resolutions and amendments
  - Veto override attempts

Confidence mapping:
  0.95  Bill signed into law / vetoed (definitive outcome)
  0.85  Bill failed / defeated on the floor
  0.75  Keyword-search match (less precise than explicit bill reference)
  0.60  Passed one chamber (outcome still uncertain)
  0.50  Bill introduced / in committee (too early to call)

Note: prob=None is returned for outcomes that have not yet resolved
(introduced, passed one chamber), so these never auto-trade.  Only
signed-into-law, vetoed, or floor-failed markets produce a tradeable signal.
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

import requests

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

_CONGRESS_BASE = "https://api.congress.gov/v3"
_TIMEOUT = 10

# Current congress number.  119th Congress: Jan 2025 – Jan 2027.
_CURRENT_CONGRESS = 119
_PREV_CONGRESS = 118

# Keywords that identify a bill-passage prediction market
_BILL_KEYWORDS = (
    "bill", "act", "legislation", "h.r.", " hr ", "s. ", "resolution",
    "amendment", "congress", "senate", "house", "passed", "signed into law",
    "become law", "veto", "filibuster", "cloture", "budget", "appropriation",
)


class CongressSource(DataSource):
    """
    Fetches US Congressional bill status from Congress.gov for markets
    about legislation passing, being signed into law, or being vetoed.
    """

    def can_handle(self, market: Market) -> bool:
        text = (market.question + " " + " ".join(market.tags)).lower()
        return (
            market.category.lower() in ("politics", "legal", "government", "general")
            and any(kw in text for kw in _BILL_KEYWORDS)
        )

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        try:
            bill_ref = self._extract_bill_reference(market.question)
            if bill_ref:
                return self._fetch_by_bill_number(bill_ref, market)
            return self._search_by_keywords(market)
        except Exception as exc:
            logger.warning("CongressSource: error for %s: %s", market.market_id, exc)
            return None

    # ── Bill reference extraction ──────────────────────────────────────────────

    def _extract_bill_reference(self, question: str) -> Optional[dict]:
        """Extract an H.R. NNNN or S. NNNN citation from the question text."""
        patterns = [
            (r"\bH\.R\.?\s*(\d+)\b",        "hr",       "house"),
            (r"\bS\.?\s+(\d+)\b",            "s",        "senate"),
            (r"\bH\.Con\.Res\.?\s*(\d+)\b",  "hconres",  "house"),
            (r"\bS\.Con\.Res\.?\s*(\d+)\b",  "sconres",  "senate"),
            (r"\bH\.Res\.?\s*(\d+)\b",       "hres",     "house"),
            (r"\bS\.Res\.?\s*(\d+)\b",       "sres",     "senate"),
            (r"\bH\.J\.Res\.?\s*(\d+)\b",    "hjres",    "house"),
            (r"\bS\.J\.Res\.?\s*(\d+)\b",    "sjres",    "senate"),
        ]
        for pattern, bill_type, chamber in patterns:
            m = re.search(pattern, question, re.IGNORECASE)
            if m:
                return {"type": bill_type, "number": m.group(1), "chamber": chamber}
        return None

    # ── Fetch by explicit bill number ─────────────────────────────────────────

    def _fetch_by_bill_number(
        self, bill_ref: dict, market: Market
    ) -> Optional[GroundTruthResult]:
        """Fetch bill details from Congress.gov API by congress/type/number."""
        for congress in (_CURRENT_CONGRESS, _PREV_CONGRESS):
            url = (
                f"{_CONGRESS_BASE}/bill/{congress}"
                f"/{bill_ref['type']}/{bill_ref['number']}"
            )
            try:
                resp = requests.get(url, params={"format": "json"}, timeout=_TIMEOUT)
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.debug("CongressSource: bill fetch failed (%s): %s", url, exc)
                continue

            bill = data.get("bill", {})
            if not bill:
                continue

            title       = bill.get("title", "")
            latest_act  = (bill.get("latestAction") or {})
            action_text = latest_act.get("text", "").lower()
            action_date = latest_act.get("actionDate", "")
            source_url  = (
                f"https://www.congress.gov/bill/{congress}th-congress"
                f"/{bill_ref['type']}/{bill_ref['number']}"
            )

            prob, conf, reasoning = self._classify_bill_status(
                action_text, market.question, title
            )

            return GroundTruthResult(
                ground_truth_prob=prob,
                confidence=conf,
                source_type=SourceType.REGULATORY,
                source_name="Congress.gov",
                source_url=source_url,
                raw_data={
                    "title": title,
                    "latest_action": action_text,
                    "action_date": action_date,
                    "bill_ref": bill_ref,
                    "congress": congress,
                },
                reasoning=reasoning,
            )

        logger.debug(
            "CongressSource: bill %s/%s not found in congresses %d or %d",
            bill_ref["type"], bill_ref["number"],
            _CURRENT_CONGRESS, _PREV_CONGRESS,
        )
        return None

    # ── Keyword search fallback ────────────────────────────────────────────────

    def _search_by_keywords(self, market: Market) -> Optional[GroundTruthResult]:
        """Search Congress.gov by keyword when no explicit bill number is found."""
        stop = {
            "will", "the", "a", "an", "be", "is", "by", "to", "of",
            "for", "and", "or", "pass", "sign", "vote", "congress",
        }
        words = [
            w for w in re.findall(r"\b[a-zA-Z]{4,}\b", market.question.lower())
            if w not in stop
        ]
        if not words:
            return None

        query = " ".join(words[:4])
        try:
            resp = requests.get(
                f"{_CONGRESS_BASE}/bill",
                params={
                    "query": query,
                    "sort": "latestAction",
                    "format": "json",
                    "limit": 3,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            results = resp.json().get("bills", [])
        except Exception as exc:
            logger.debug("CongressSource: keyword search failed: %s", exc)
            return None

        if not results:
            return None

        best = results[0]
        title       = best.get("title", "")
        action_text = (best.get("latestAction") or {}).get("text", "").lower()
        url         = best.get("url", _CONGRESS_BASE)

        prob, conf, reasoning = self._classify_bill_status(
            action_text, market.question, title
        )

        # Keyword matches are inherently less reliable — cap at 0.75
        # so they can pass the 0.80 confidence gate only if the action
        # is definitively final (signed/vetoed).
        conf = min(conf, 0.75)

        return GroundTruthResult(
            ground_truth_prob=prob,
            confidence=conf,
            source_type=SourceType.REGULATORY,
            source_name="Congress.gov (keyword search)",
            source_url=url,
            raw_data={"title": title, "latest_action": action_text},
            reasoning=f"Congress.gov keyword search: {reasoning}",
        )

    # ── Bill status classifier ────────────────────────────────────────────────

    def _classify_bill_status(
        self, action_text: str, question: str, bill_title: str
    ) -> Tuple[Optional[float], float, str]:
        """
        Classify a bill's status from its latest action text.

        Returns (ground_truth_prob, confidence, reasoning).
        prob=None means the outcome is not yet known (no trade fired).
        """
        q = action_text  # already lower-cased by callers

        # ── Question intent ───────────────────────────────────────────────────
        question_lower = question.lower()
        wants_passage = any(w in question_lower for w in (
            "pass", "sign", "enact", "become law", "approve", "enacted", "signed",
        ))
        wants_failure = any(w in question_lower for w in (
            "fail", "veto", "block", "reject", "die", "killed", "defeated",
        ))

        # ── Classify latest action ────────────────────────────────────────────
        signed_law = any(p in q for p in (
            "became public law", "signed by president", "signed into law",
            "enacted", "became law",
        ))
        vetoed = "vetoed" in q
        failed = any(p in q for p in (
            "failed", "defeated", "motion to table agreed to", "tabled",
            "failed of passage",
        ))
        passed_chamber = any(p in q for p in (
            "passed house", "passed senate", "passed by the",
            "agreed to in senate", "agreed to in house",
        ))
        introduced = any(p in q for p in (
            "introduced in", "referred to committee", "referred to the committee",
            "received in the senate", "referred to",
        ))

        # ── Map status to probability ─────────────────────────────────────────
        if signed_law:
            prob = 1.0 if wants_passage else (0.0 if wants_failure else None)
            conf = 0.95
            reasoning = (
                f"Bill SIGNED INTO LAW. "
                f"Title: '{bill_title[:80]}'. "
                f"Latest action: '{action_text[:100]}'"
            )
        elif vetoed:
            prob = 0.0 if wants_passage else (1.0 if wants_failure else None)
            conf = 0.90
            reasoning = (
                f"Bill VETOED by President. "
                f"Title: '{bill_title[:80]}'. "
                f"Latest action: '{action_text[:100]}'"
            )
        elif failed:
            prob = 0.0 if wants_passage else (1.0 if wants_failure else None)
            conf = 0.85
            reasoning = (
                f"Bill FAILED on floor. "
                f"Title: '{bill_title[:80]}'. "
                f"Latest action: '{action_text[:100]}'"
            )
        elif passed_chamber:
            prob = None  # passed one chamber — not yet law, outcome uncertain
            conf = 0.60
            reasoning = (
                f"Bill passed one chamber (not yet law). "
                f"Latest action: '{action_text[:100]}'"
            )
        elif introduced:
            prob = None  # still in committee — too early to call
            conf = 0.50
            reasoning = (
                f"Bill introduced / in committee. "
                f"Latest action: '{action_text[:100]}'"
            )
        else:
            prob = None
            conf = 0.50
            reasoning = f"Bill status unclear. Latest action: '{action_text[:100]}'"

        return prob, conf, reasoning
