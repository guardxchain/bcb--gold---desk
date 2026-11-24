"""Stress tests for the BCB Gold Desk. Run: python stress_test.py
Uses a throwaway ledger, fake alerts and fake prices. No network, no broker."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DESK_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
import desk  # noqa: E402

CFG = desk.load_config()
RESULTS = []


def fresh():
    if os.path.exists(desk.DB_PATH):
        os.remove(desk.DB_PATH)
    return desk.db()


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def alert(now, side="BUY", price=2650.0, stop=2645.0, **extra):
    a = {"symbol": "XAUUSD", "side": side, "price": price, "stop": stop,
         "time": now.isoformat().replace("+00:00", "Z"), "trend_4h": "UP" if side == "BUY" else "DOWN",
         "atr": 4.0, "level_distance": 1.5, "gbpusd": 1.27}
    a.update(extra)
    return a


def tick(con, now, price, high=None, low=None):
    return desk.handle_tick({"price": price, "high": high or price, "low": low or price}, CFG, con, now)


def check(name, condition, what_happened):
    RESULTS.append((name, "PASS" if condition else "FAIL", what_happened))


NOW = at("2026-11-24T10:00:00")  # Tuesday 10:00 UK (GMT) - London session

# 1 Duplicate orders
con = fresh(); tick(con, NOW, 2650)
a = alert(NOW)
r1 = desk.handle_alert(a, CFG, con, NOW)
r2 = desk.handle_alert(a, CFG, con, NOW + timedelta(seconds=3))
trades = con.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
check("Duplicate orders", r2["scout"]["status"] == "DUPLICATE" and trades == 1,
      f"Same alert sent twice: 1st {r1['pilot']['result']}, 2nd {r2['scout']['status']}, trades open = {trades}")

# 1b Approval reused
con = fresh(); tick(con, NOW, 2650)
card = desk.scout(alert(NOW), CFG, NOW)
ap = desk.shield(card, CFG, con, NOW)
p1 = desk.pilot(card, ap, CFG, con, NOW)
con.execute("UPDATE trades SET status='LOSS', pnl_gbp=0"); con.commit()
p2 = desk.pilot(card, ap, CFG, con, NOW)
check("Approval reused", p2["result"] == "REFUSED", f"Same Shield approval used twice: 2nd = {p2['reason']}")

# 2 Missed stop
con = fresh(); tick(con, NOW, 2650)
r = desk.handle_alert(alert(NOW, stop=None), CFG, con, NOW)
check("Missing stop", r["pilot"] is None and r["scout"]["status"] == "STALE",
      f"Alert with no stop: Scout {r['scout']['status']} ({r['scout']['reason']}), no trade")
r = desk.handle_alert(alert(NOW, price=2650.5, stop=2655), CFG, con, NOW)
check("Stop on wrong side", r["pilot"] is None, f"BUY with stop above entry: {r['scout']['reason']}")

# 3 Wrong position size
con = fresh(); tick(con, NOW, 2650)
r = desk.handle_alert(alert(NOW, stop=2649.9), CFG, con, NOW)
check("Stop far too tight", r["pilot"] is None and r["shield"]["decision"] == "BLOCK",
      f"10-cent stop would need huge size: {r['shield']['reason']}")
con = fresh(); tick(con, NOW, 2650)
r = desk.handle_alert(alert(NOW), CFG, con, NOW)
risk = r["shield"]["risk_gbp"]
check("Size matches 5% risk", 240 <= risk <= 250 and r["shield"]["lots"] == 0.63,
      f"$5 stop on £5,000: {r['shield']['lots']} lots, real risk £{risk} (limit £250)")

# 4 Stale price feed
con = fresh()
r = desk.handle_alert(alert(NOW - timedelta(minutes=5)), CFG, con, NOW)
check("Stale alert", r["scout"]["status"] == "STALE", f"5-minute-old alert: {r['scout']['reason']}")
con = fresh(); tick(con, NOW - timedelta(seconds=90), 2650)
r = desk.handle_alert(alert(NOW), CFG, con, NOW)
check("Stale price feed", r["shield"]["reason"].startswith("Price feed"),
      f"Last price 90s old: Shield {r['shield']['decision']} - {r['shield']['reason']}")
con = fresh()
desk.watchdog_once(CFG, con, NOW)
warned = desk.get_state(con, "feed_warned")
check("Feed dies in session", warned is True, "No prices in London session: watchdog sent 'feed has gone quiet' warning")

# 5 API timeout (Telegram unreachable)
os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "fake", "1"
start = datetime.now()
try:
    ok = desk.send_telegram("test", wait=True)
    crashed = False
except Exception:
    ok, crashed = False, True
secs = (datetime.now() - start).total_seconds()
os.environ.pop("TELEGRAM_BOT_TOKEN"); os.environ.pop("TELEGRAM_CHAT_ID")
check("Message API timeout", not crashed and ok is False and secs <= 12,
      f"Telegram unreachable: desk kept running, gave up after {secs:.1f}s")

# 6 Timezone
summer_ok = desk.in_session(CFG, at("2026-07-14T06:30:00"))   # 07:30 BST
winter_ok = desk.in_session(CFG, at("2026-01-13T06:30:00"))   # 06:30 GMT
weekend = desk.in_session(CFG, at("2026-11-28T10:00:00"))     # Saturday
check("Timezone (BST/GMT)", summer_ok == "London" and winter_ok is None and weekend is None,
      f"06:30 UTC in July = {summer_ok} session; 06:30 UTC in January = {winter_ok}; Saturday = {weekend}")

# 7 Partial fill - paper fills are all-or-nothing
RESULTS.append(("Partial fill", "N/A ", "Not testable on paper (fills are whole). Retest at the cTrader demo stage"))

# 8 Two losses -> STOP ALL
con = fresh(); t = NOW
for i in range(2):
    tick(con, t, 2650)
    desk.handle_alert(alert(t, price=2650 + i * 0.01), CFG, con, t)
    tick(con, t + timedelta(seconds=30), 2644, low=2644)
    t += timedelta(minutes=5)
tick(con, t, 2650)
r = desk.handle_alert(alert(t), CFG, con, t)
pnl = desk.day_pnl(con, t)
check("2 losses -> STOP ALL", r["shield"]["decision"] == "BLOCK" and desk.stopped_today(con, t),
      f"Two stopped-out trades = £{pnl:.2f}; 3rd signal: {r['shield']['reason']}")

# 9 One 3R win -> +15% -> stop for day
con = fresh(); tick(con, NOW, 2650)
desk.handle_alert(alert(NOW), CFG, con, NOW)
tick(con, NOW + timedelta(minutes=20), 2666, high=2666)
pnl = desk.day_pnl(con, NOW)
t2 = NOW + timedelta(minutes=30); tick(con, t2, 2660)
r = desk.handle_alert(alert(t2, price=2660, stop=2655), CFG, con, t2)
check("Target hit -> stop for day", r["shield"] is not None and r["shield"]["decision"] == "BLOCK",
      f"One 3R win = £{pnl:+.2f} after estimated costs; next signal: {r['shield']['reason']}")

# 10 Stop and target in the same candle -> counted as a loss
con = fresh(); tick(con, NOW, 2650)
desk.handle_alert(alert(NOW), CFG, con, NOW)
tick(con, NOW + timedelta(minutes=1), 2655, high=2666, low=2644)
status = con.execute("SELECT status FROM trades").fetchone()["status"]
check("Stop+target same candle", status == "LOSS", f"Candle hit both: counted as {status} (worst case)")

# 11 Live mode refused
con = fresh(); tick(con, NOW, 2650)
live = dict(CFG, mode="LIVE", live_trading=True)
card = desk.scout(alert(NOW), live, NOW); ap = desk.shield(card, live, con, NOW)
p = desk.pilot(card, ap, live, con, NOW)
check("No live trading", p["result"] == "REFUSED", f"Config switched to LIVE: Pilot {p['result']} - {p['reason']}")

# 12 Daily report
con = fresh(); tick(con, NOW, 2650)
desk.handle_alert(alert(NOW), CFG, con, NOW)
rep = desk.daily_report(CFG, con, NOW)
check("Daily report", "Balance" in rep and "Decision" in rep, rep.replace("\n", " | "))

w = max(len(n) for n, _, _ in RESULTS)
for name, res, what in RESULTS:
    print(f"{res}  {name.ljust(w)}  {what}")
print(f"\n{sum(r == 'PASS' for _, r, _ in RESULTS)}/{len(RESULTS)} passed")
