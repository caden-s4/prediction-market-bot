# Save as runthis2.py
import json
lines = [json.loads(l) for l in open('ghost_trades.jsonl')]
entries = {}
for e in lines:
    if e['event'] == 'entry':
        entries[e['market_id']] = e
w = 0
lo = 0
ot = 0
for ex in lines:
    if ex['event'] != 'exit':
        continue
    en = entries.get(ex['market_id'], {})
    ts = ex.get('ts', '')
    if ts < '2026-04-01':
        continue
    pnl = ex.get('pnl', 0)
    if pnl > 0:
        w += 1
    elif pnl < 0:
        lo += 1
    else:
        ot += 1
    act = en.get('action', '?')
    gtp = en.get('gt_prob', '?')
    ep = en.get('entry_price', '?')
    xp = ex.get('exit_price', '?')
    reason = ex.get('exit_reason', '?')
    q = en.get('question', '')[:70]
    print(f"{ts[:19]} | {ex['market_id']} | {act} | gt={gtp} | entry={ep} | exit={xp} | pnl={pnl:.2f} | {reason} | {q}")
print(f"\nPost-fix: {w}W / {lo}L / {ot} other")