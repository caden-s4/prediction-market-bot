"""
resolution/ – Bot 2: Resolution Drift Arbitrage.

Scans non-crypto markets on Polymarket + Kalshi expiring within 24 hours.
Finds markets where the real-world outcome is already determinable from a hard
data source but hasn't been priced in yet. Buys the correct side, exits at
80%+ of theoretical gain or holds to resolution.

Entry point: resolution.executor.ResolutionBot (polling, every 5 min).
"""
