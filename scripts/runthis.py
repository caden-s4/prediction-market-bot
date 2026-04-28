"""Trace EVERY log line mentioning the market."""
from pathlib import Path

LOG_DIR = Path(r"C:\Users\caden\Desktop\prediction_market_bot\logs")
MARKET = "KXNBAGAME-26APR14MIACHA-MIA"

found = []
log_files = sorted(LOG_DIR.glob("bot.log*"))
print(f"Searching {len(log_files)} log file(s):")
for log in log_files:
    print(f"  {log.name} ({log.stat().st_size:,} bytes)")

for log in log_files:
    with open(log, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if MARKET in line:
                found.append(line.rstrip())

print(f"\nTotal matches: {len(found)}\n")
for line in found:
    print(line)