"""
shared/ – infrastructure used by both Bot 1 (maker) and Bot 2 (resolution drift).

  fee_cache       : query + cache per-market fee rates; refresh every 15 min
  exclusion_list  : dynamic list of markets the bot must never touch
  bankroll        : 60/40 split bankroll manager; enforces per-bot allocation caps
"""
