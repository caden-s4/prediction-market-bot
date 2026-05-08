Three hot-path invariants added at: (1) REST parser `kalshi.py` after yes_price computation — skips market on out-of-range mid; (2) WS ticker handler `kalshi_ws.py` — skips snapshot on out-of-range mid; (3) scanner T2 refresh `scanner.py` where WS overrides REST — logs disagreement >0.05 delta; (4) gap detector `gap_detector.py` after raw_gap computation — logs but does not block gaps >0.40. All emit `gate=invariant_violation` events.

One-cycle observation: 0 `kalshi_mid_out_of_range`, 0 `implausible_gap`, 9 `ws_rest_mid_disagreement` (real — OTM brackets where REST bulk-API returns stale 0.25–0.51, WS orderbook has true price 0.01–0.03; scanner correctly uses WS, invariant makes the divergence visible).

The gate_funnel script needs an update to surface `invariant_violation` as its own category alongside `scanner_reject`, `gt_routing`, `confidence`, `executor_pretrade`, `snipe` — do not make the change yet.
