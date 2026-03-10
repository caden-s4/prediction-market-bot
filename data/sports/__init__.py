"""
data.sports – live sports signal pipeline for latency/reaction-speed trading.

Components:
  LiveGameMonitor   – polls ESPN for in-progress game state every 15s
  WinProbabilityModel – converts game state to win probability (<5ms, pure math)
  ShockDetector     – flags large probability jumps between polling cycles
  MarketMatcher     – fuzzy-matches Kalshi market titles to ESPN game objects
"""
