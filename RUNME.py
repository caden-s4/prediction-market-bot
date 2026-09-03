from data.markets.kalshi import KalshiClient
from config import AppConfig

cfg = AppConfig.load()
c = KalshiClient(
    api_key=cfg.kalshi.api_key,
    api_secret=cfg.kalshi.api_secret,
    base_url=cfg.kalshi.base_url,
)

m = c.get_market("KXHIGHTPHX-26APR29-T94")
if m is None:
    print("Market not found")
else:
    print("rules_primary:", m.raw.get("rules_primary"))
    print()
    print("rules_secondary:", m.raw.get("rules_secondary"))
    print()
    # Show all keys so we don't miss the right field name
    rule_keys = [k for k in m.raw if "rule" in k.lower()]
    print("rule-related keys in raw:", rule_keys)
