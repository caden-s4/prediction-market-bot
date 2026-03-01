"""
data.ground_truth.federal_register – regulatory filing fetcher.

Source: api.federalregister.gov (free, no API key required)

Covers:
  - Final rules and regulations (rule published = effective)
  - Enforcement actions with clear outcome language
  - Interim final rules (effective but challengeable)
  - Proposed rules (filed but not yet decided)
  - Agency guidance documents (non-binding, no signal)
  - Executive orders
  - Presidential documents

Document type → confidence mapping:
  Final Rule                                   RULE (standard)   0.90
  Enforcement Action / Consent Order / Penalty RULE (with kws)   0.85
  Interim Final Rule                           RULE (with kws)   0.75
  Proposed Rule                                PRORULE           None  (outcome unknown)
  Guidance Document / Advisory                 NOTICE (with kws) None  (non-binding)
  Other Notice / Presidential Document         NOTICE/PRESDOCU   0.50

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
    "etf approval", "etf rejection",
    # NOTE: "ipo" intentionally omitted — IPO S-1 filings live on SEC EDGAR,
    # not on federalregister.gov.  Including "ipo" only caused false matches
    # that returned confidence=0.50/prob=None for every IPO market.
)


class FederalRegisterSource(DataSource):
    """
    Fetches regulatory and legal filing data for markets about US government
    decisions, court rulings, and agency actions.
    """

    def can_handle(self, market: Market) -> bool:
        # Sports markets are never resolved by government documents.
        if market.category.lower() in ("sports", "sport", "esports"):
            return False
        # Bracket/count series ("will X or more judges be fired?") ask for a
        # running count, not a binary regulatory outcome.  The Federal Register
        # API cannot compute a count from regulatory filings, so these markets
        # always return prob=None — skip them entirely to save API quota and
        # cycle time.  Examples: KXJUDGECOUNT, KXFIREDCOUNT bracket series.
        text = (market.question + " " + " ".join(market.tags)).lower()
        _COUNT_PHRASES = (" or more", " or fewer", " at least ", " at most ", "how many")
        if any(p in text for p in _COUNT_PHRASES):
            return False
        # Require at least one domain-specific keyword in the question/tags.
        # A bare category match (e.g. category="politics") is not sufficient —
        # it causes false positives for political-figure markets ("Will Bill
        # Clinton appear in public?") that have no Federal Register documents,
        # burning API quota and producing confidence=0.50/prob=None noise.
        return (
            any(kw in text for kw in _REGULATORY_KEYWORDS)
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
        title_lower = title.lower()

        # Classify by document type then refine by title keywords.
        if doc_type == "RULE":
            if any(kw in title_lower for kw in (
                "interim final", "interim rule", "temporary rule",
            )):
                # Interim Final Rule — effective immediately but subject to
                # challenge and withdrawal; more uncertain than a final rule.
                ground_truth_prob = self._rule_matches_yes(market.question, title)
                confidence = 0.75
                source_type = SourceType.REGULATORY
                doc_label = "INTERIM FINAL RULE"
                reasoning = (
                    f"Federal Register INTERIM FINAL RULE: '{title}' "
                    f"published {pub_date}. Effective but challengeable; "
                    f"confidence capped at 0.75."
                )
            elif any(kw in title_lower for kw in (
                "enforcement", "penalty", "consent order",
                "cease and desist", "civil money penalty",
            )):
                # Enforcement action with clear outcome language.  Confidence
                # is 0.85 only when we can parse the direction; 0.50 otherwise.
                ground_truth_prob = self._rule_matches_yes(market.question, title)
                confidence = 0.85 if ground_truth_prob is not None else 0.50
                source_type = SourceType.REGULATORY
                doc_label = "ENFORCEMENT ACTION"
                reasoning = (
                    f"Federal Register ENFORCEMENT ACTION: '{title}' "
                    f"published {pub_date}. "
                    + ("Direction parsed from title." if ground_truth_prob is not None
                       else "Direction ambiguous — prob=None, auto-trade blocked.")
                )
            else:
                # Standard Final Rule — highest regulatory confidence.
                ground_truth_prob = self._rule_matches_yes(market.question, title)
                confidence = 0.90
                source_type = SourceType.REGULATORY
                doc_label = "FINAL RULE"
                reasoning = (
                    f"Federal Register FINAL RULE: '{title}' "
                    f"published {pub_date}. Authoritative hard source."
                )

        elif doc_type == "PRORULE":
            # Proposed rule — filed but outcome is not yet decided.
            ground_truth_prob = None
            confidence = 0.60
            source_type = SourceType.REGULATORY
            doc_label = "PROPOSED RULE"
            reasoning = (
                f"Federal Register PROPOSED RULE: '{title}' "
                f"published {pub_date}. Outcome not yet determinable."
            )

        elif doc_type == "NOTICE":
            if any(kw in title_lower for kw in (
                "guidance", "guidance document", "advisory", "policy statement",
                "frequently asked questions", "faq",
            )):
                # Guidance documents are non-binding and carry no predictive
                # signal about regulatory outcomes.
                logger.debug(
                    "FederalRegister: guidance document, skipping %s", market.market_id
                )
                return None
            ground_truth_prob = None
            confidence = 0.50
            source_type = SourceType.AGGREGATED
            doc_label = "NOTICE"
            reasoning = f"Federal Register NOTICE: '{title}' published {pub_date}."

        else:
            # Presidential documents, executive orders, and other types.
            ground_truth_prob = None
            confidence = 0.50
            source_type = SourceType.AGGREGATED
            doc_label = doc_type or "DOCUMENT"
            reasoning = f"Federal Register {doc_label}: '{title}' published {pub_date}."

        return GroundTruthResult(
            ground_truth_prob=ground_truth_prob,
            confidence=confidence,
            source_type=source_type,
            source_name="Federal Register API",
            source_url=url,
            raw_data={**best, "doc_label": doc_label},
            reasoning=reasoning,
        )

    def _rule_matches_yes(self, question: str, rule_title: str) -> Optional[float]:
        """
        Determine whether a published regulatory document means YES or NO for
        the market question.

        Previous approach: required the SAME approve/reject vocabulary to appear
        in BOTH the question AND the document title simultaneously.  This was
        far too strict — market questions use informal phrasing ("Will X be
        passed?") while Federal Register titles use bureaucratic language
        ("Final Rule: Implementation of Regulatory Framework for…").  The
        vocabulary rarely overlaps, causing almost every Final Rule to return
        None even when the answer was obvious.

        New approach: evaluate question intent and document outcome independently.

        QUESTION INTENT — what is the market asking about?
          approve_intent : question asks whether the rule will be adopted/enacted
          reject_intent  : question asks whether the rule will be blocked/revoked

        DOCUMENT OUTCOME — what does this document represent?
          A published FINAL RULE inherently means the rule was APPROVED/ENACTED.
          The only exception: if the document title contains revocation/repeal
          language, the rule was rescinded (rejection outcome).

        PROBABILITY MAPPING:
          approve_intent + approval outcome → 1.0 (rule published = YES)
          approve_intent + rejection outcome → 0.0 (rule revoked = NO)
          reject_intent  + approval outcome → 0.0 (rule published = NO for rejection Q)
          reject_intent  + rejection outcome → 1.0 (rule revoked = YES for rejection Q)
          ambiguous intent (both or neither) → None (skip trade)
        """
        q = question.lower()
        t = rule_title.lower()

        # ── Question intent ───────────────────────────────────────────────────
        approve_intent = any(w in q for w in (
            "approve", "pass", "enact", "finalize", "adopt", "publish", "sign",
            "implement", "take effect", "go into effect", "enacted", "confirmed",
            "ratif",  # ratify / ratified
        ))
        reject_intent = any(w in q for w in (
            "reject", "deny", "block", "ban", "revoke", "repeal", "withdraw",
            "overturn", "struck down", "vacate", "enjoin", "halt", "suspend",
            "veto", "fail", "kill",
        ))

        # ── Document outcome ──────────────────────────────────────────────────
        # A published Final Rule is an approval/enactment by default.
        # Title revocation/repeal words flip this to a rejection outcome.
        doc_is_rejection = any(w in t for w in (
            "revoc", "repeal", "withdrawal", "rescission", "termination",
            "vacatur", "removal of", "elimination of",
        ))
        doc_is_approval = not doc_is_rejection

        # ── Map to probability ────────────────────────────────────────────────
        if approve_intent and not reject_intent:
            return 1.0 if doc_is_approval else 0.0
        if reject_intent and not approve_intent:
            return 0.0 if doc_is_approval else 1.0
        # Both signals or neither — question intent is ambiguous; skip the trade.
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
