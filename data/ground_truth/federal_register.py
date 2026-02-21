"""
data.ground_truth.federal_register – regulatory filing fetcher.

Source: api.federalregister.gov (free, no API key required)

Covers:
  - Final rules and regulations (rule published = effective)
  - Proposed rules (filed = proposed, not yet final)
  - Executive orders
  - Presidential documents
  - Agency notices

Confidence: 0.90 for a published final rule; 0.80 for a proposed rule filing.

Note on PACER (federal court filings): PACER requires login credentials and
per-page fees. For court outcomes we use CourtListener (free public API) which
mirrors PACER data for most federal courts.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

_FR_BASE = "https://www.federalregister.gov/api/v1"
_CL_BASE = "https://www.courtlistener.com/api/rest/v3"
_TIMEOUT = 10

# Keywords that suggest a federal regulatory market
_REGULATORY_KEYWORDS = (
    "fda", "epa", "sec", "fdic", "occ", "federal reserve", "fomc",
    "cfpb", "ftc", "doj", "dod", "hhs", "cms", "irs",
    "regulation", "rule", "ruling", "ban", "approve", "reject",
    "executive order", "eo ", "federal register",
    "merger", "acquisition", "antitrust",
)

_COURT_KEYWORDS = (
    "court", "ruling", "judge", "verdict", "lawsuit", "case",
    "supreme court", "circuit", "appeal", "indictment", "conviction",
    "sentence", "trial", "plea",
)

_SEC_KEYWORDS = (
    "sec ", "securities", "crypto etf", "bitcoin etf", "ethereum etf",
    "etf approval", "etf rejection", "ipo",
)


class FederalRegisterSource(DataSource):
    """
    Fetches regulatory and legal filing data for markets about US government
    decisions, court rulings, and agency actions.
    """

    def can_handle(self, market: Market) -> bool:
        text = (market.question + " " + " ".join(market.tags)).lower()
        return (
            market.category.lower() in ("politics", "legal", "regulatory", "law", "government")
            or any(kw in text for kw in _REGULATORY_KEYWORDS)
            or any(kw in text for kw in _COURT_KEYWORDS)
            or any(kw in text for kw in _SEC_KEYWORDS)
        )

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        try:
            # Try Federal Register first (regulatory/rule markets)
            result = self._search_federal_register(market)
            if result:
                return result

            # Try CourtListener for court-related markets
            result = self._search_court_listener(market)
            if result:
                return result

            return None

        except Exception as exc:
            logger.warning(
                "FederalRegisterSource: error for %s: %s", market.market_id, exc
            )
            return None

    # ── Federal Register ──────────────────────────────────────────────────────

    def _search_federal_register(self, market: Market) -> Optional[GroundTruthResult]:
        """Search Federal Register documents API for relevant filings."""
        terms = self._extract_search_terms(market.question)
        if not terms:
            return None

        params = {
            "conditions[term]": terms,
            "conditions[type][]": ["RULE", "PRORULE", "NOTICE", "PRESDOCU"],
            "order": "newest",
            "per_page": 5,
            "fields[]": [
                "title", "document_number", "publication_date",
                "type", "agencies", "abstract", "html_url",
            ],
        }

        resp = requests.get(f"{_FR_BASE}/documents.json", params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])

        if not results:
            logger.debug("FederalRegister: no results for terms '%s'", terms)
            return None

        best = results[0]
        doc_type = best.get("type", "")
        title = best.get("title", "")
        pub_date = best.get("publication_date", "")
        url = best.get("html_url", f"{_FR_BASE}/documents")

        # Determine outcome and confidence based on document type
        if doc_type == "RULE":
            # Final rule = decision is made and published
            ground_truth_prob = self._rule_matches_yes(market.question, title)
            confidence = 0.90
            source_type = SourceType.REGULATORY
            reasoning = (
                f"Federal Register FINAL RULE found: '{title}' "
                f"published {pub_date}. This is an authoritative hard source."
            )
        elif doc_type == "PRORULE":
            # Proposed rule = filed but not yet final
            ground_truth_prob = None  # can't determine outcome from a proposal
            confidence = 0.60
            source_type = SourceType.REGULATORY
            reasoning = (
                f"Federal Register PROPOSED RULE found: '{title}' "
                f"published {pub_date}. Outcome not yet determinable."
            )
        else:
            ground_truth_prob = None
            confidence = 0.50
            source_type = SourceType.AGGREGATED
            reasoning = f"Federal Register notice/document found: '{title}' ({doc_type})."

        return GroundTruthResult(
            ground_truth_prob=ground_truth_prob,
            confidence=confidence,
            source_type=source_type,
            source_name="Federal Register API",
            source_url=url,
            raw_data=best,
            reasoning=reasoning,
        )

    def _rule_matches_yes(self, question: str, rule_title: str) -> Optional[float]:
        """
        Attempt to determine if a final rule means YES or NO for the market.

        This is a best-effort heuristic. If uncertain, return None (skip the trade).
        """
        q = question.lower()
        t = rule_title.lower()

        # Look for approval/denial language in question vs rule title
        approve_words = ("approve", "pass", "enact", "finalize", "adopt", "publish")
        reject_words = ("reject", "deny", "block", "ban", "revoke", "repeal", "withdraw")

        q_approves = any(w in q for w in approve_words)
        q_rejects = any(w in q for w in reject_words)
        t_approves = any(w in t for w in approve_words)
        t_rejects = any(w in t for w in reject_words)

        if q_approves and t_approves:
            return 1.0
        if q_approves and t_rejects:
            return 0.0
        if q_rejects and t_rejects:
            return 1.0
        if q_rejects and t_approves:
            return 0.0
        # Ambiguous – a rule exists but we can't tell if it's the YES outcome
        return None

    # ── CourtListener ─────────────────────────────────────────────────────────

    def _search_court_listener(self, market: Market) -> Optional[GroundTruthResult]:
        """Search CourtListener for court opinions/orders relevant to market."""
        text = (market.question + " " + " ".join(market.tags)).lower()
        if not any(kw in text for kw in _COURT_KEYWORDS):
            return None

        terms = self._extract_search_terms(market.question)
        params = {
            "q": terms,
            "type": "o",  # opinions
            "order_by": "score desc",
            "stat_Precedential": "on",
        }
        try:
            resp = requests.get(
                "https://www.courtlistener.com/api/rest/v3/search/",
                params=params,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
        except Exception as exc:
            logger.debug("CourtListener: search failed: %s", exc)
            return None

        if not results:
            return None

        best = results[0]
        case_name = best.get("caseName", "")
        date_filed = best.get("dateFiled", "")
        score = best.get("score", 0)
        absolute_url = best.get("absolute_url", "")

        if score < 5:
            return None  # weak match

        return GroundTruthResult(
            ground_truth_prob=None,  # ruling direction requires full text parsing
            confidence=0.75,
            source_type=SourceType.REGULATORY,
            source_name="CourtListener",
            source_url=f"https://www.courtlistener.com{absolute_url}",
            raw_data=best,
            reasoning=(
                f"Court opinion found: '{case_name}' filed {date_filed}. "
                f"Manual review required to determine YES/NO direction. "
                f"Confidence=0.75 but prob=None – skipping auto-trade."
            ),
        )

    # ── Term extraction ───────────────────────────────────────────────────────

    def _extract_search_terms(self, question: str) -> str:
        """Extract 2-4 key terms from the market question for search."""
        # Remove common question words
        stop = {
            "will", "the", "a", "an", "be", "is", "are", "was", "were",
            "by", "in", "on", "at", "to", "of", "for", "and", "or",
            "before", "after", "this", "that", "its", "their",
            "have", "has", "had", "does", "do", "did", "not",
            "yes", "no", "any", "all", "get", "make", "take",
        }
        words = re.findall(r"\b[a-zA-Z]{4,}\b", question.lower())
        key_words = [w for w in words if w not in stop]
        # Return the first 4 most meaningful words
        return " ".join(key_words[:4])
