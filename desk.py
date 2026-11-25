"""BCB Gold Desk - Scout, Shield, paper Pilot, Journal and Coordinator messages.

Runs on Railway. Standard Python only (no extra packages).
TradingView sends two kinds of webhook to this service:
  POST /alert  - a BUY/SELL signal (Scout -> Shield -> Pilot)
  POST /tick   - the latest 1-minute candle (keeps prices fresh, manages paper trades)
GET /health and GET /status are for checking it is alive.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

VERSION = "1.2.2"
UK = ZoneInfo("Europe/London")
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("DESK_CONFIG", os.path.join(HERE, "bcbdesk_config.yaml"))
DB_PATH = os.environ.get("DESK_DB", os.path.join(HERE, "desk.db"))
LOCK = threading.RLock()
STARTED = time.time()
GRADES = {"A": 3, "B": 2, "C": 1}


# ---------------------------------------------------------------- config
def _value(raw):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [_value(x) for x in raw[1:-1].split(",") if x.strip()]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    for kind in (int, float):
        try:
            return kind(raw)
        except ValueError:
            pass
    return raw


def load_config(path=None):
    cfg = {}
    with open(path or CONFIG_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.split(" #")[0].strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, raw = line.split(":", 1)
            cfg[key.strip()] = _value(raw)
    return cfg


# ---------------------------------------------------------------- ledger
def db():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, agent TEXT,
            event TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS alerts(alert_id TEXT PRIMARY KEY, ts TEXT);
        CREATE TABLE IF NOT EXISTS approvals(approval_id TEXT PRIMARY KEY, ts REAL, used INTEGER);
        CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, opened TEXT, uk_day TEXT,
            side TEXT, entry REAL, stop REAL, target REAL, lots REAL, risk_gbp REAL,
            status TEXT, closed TEXT, exit REAL, pnl_gbp REAL, r REAL, reason TEXT, gbpusd REAL);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    return con


def journal(con, agent, event, detail, now):
    con.execute("INSERT INTO events(ts, agent, event, detail) VALUES (?,?,?,?)",
                (now.isoformat(), agent, event, json.dumps(detail, default=str)))
    con.commit()


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(con, key, value):
    con.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?,?)", (key, json.dumps(value)))
    con.commit()


# ---------------------------------------------------------------- time helpers
def parse_time(value):
    """TradingView {{timenow}} is ISO like 2026-11-23T10:15:00Z. Epoch ms also accepted."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def uk_day(now):
    return now.astimezone(UK).date().isoformat()


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_session(cfg, now):
    local = now.astimezone(UK)
    if local.weekday() >= 5:
        return None
    mins = local.hour * 60 + local.minute
    for name in cfg.get("sessions_allowed", []):
        window = cfg.get(f"{name.lower()}_hours")
        if not window:
            continue
        start, end = window.split("-")
        if _minutes(start) <= mins < _minutes(end):
            return name
    return None


# ---------------------------------------------------------------- Telegram (Coordinator voice)
def send_telegram(text, wait=False):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[telegram off]", text)
        return False

    def _send():
        try:
            body = json.dumps({"chat_id": chat, "text": text}).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                         data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            return True
        except Exception as exc:  # never let Telegram crash the desk
            print("[telegram error]", exc)
            return False

    if wait:
        return _send()
    threading.Thread(target=_send, daemon=True).start()
    return True


# ---------------------------------------------------------------- SCOUT
REQUIRED = ("symbol", "side", "price", "stop", "time", "trend_4h", "atr", "level_distance")


def scout(alert, cfg, now):
    missing = [k for k in REQUIRED if alert.get(k) in (None, "")]
    if missing:
        return {"status": "STALE", "reason": "Missing from alert: " + ", ".join(missing)}
    try:
        sent = parse_time(alert["time"])
        price, stop = float(alert["price"]), float(alert["stop"])
        atr, level_distance = float(alert["atr"]), abs(float(alert["level_distance"]))
    except (ValueError, TypeError) as exc:
        return {"status": "STALE", "reason": f"Unreadable alert value ({exc})"}
    age = (now - sent).total_seconds()
    if age > cfg["alert_max_age_seconds"] or age < -30:
        return {"status": "STALE", "reason": f"Alert is {int(age)}s old", "alert_time": sent.isoformat()}

    side = str(alert["side"]).upper()
    if side not in ("BUY", "SELL") or str(alert["symbol"]).upper().replace("/", "") != cfg["market"]:
        return {"status": "NO SETUP", "reason": "Wrong side or symbol"}
    direction = 1 if side == "BUY" else -1
    risk_usd = (price - stop) * direction
    if risk_usd <= 0:
        return {"status": "NO SETUP", "reason": "Stop is on the wrong side of the entry"}

    trend_ok = str(alert["trend_4h"]).upper() == ("UP" if side == "BUY" else "DOWN")
    level_ok = level_distance <= atr
    session = in_session(cfg, now)
    news_ok = not bool(alert.get("news"))
    passed = sum([trend_ok, level_ok, bool(session), news_ok])
    grade = "A" if passed == 4 else "B" if passed == 3 else "C"
    tp_r = cfg.get("take_profit_r") or 0
    target = price + direction * risk_usd * tp_r if tp_r > 0 else None
    notes = [("with" if trend_ok else "against") + " the 4H trend",
             ("near" if level_ok else "away from") + " a key level",
             f"{session} session" if session else "outside sessions",
             "no news flag" if news_ok else "NEWS flag"]
    return {"status": "SETUP", "side": side, "entry": round(price, 2), "stop": round(stop, 2),
            "target": round(target, 2) if target else None, "risk_usd": round(risk_usd, 2), "grade": grade,
            "session": session, "reason": f"{side} {grade}: " + ", ".join(notes),
            "alert_time": sent.isoformat(), "gbpusd": float(alert.get("gbpusd") or cfg["gbpusd_estimate"])}


# ---------------------------------------------------------------- ledger maths
def balance(con, cfg):
    total = con.execute("SELECT COALESCE(SUM(pnl_gbp),0) s FROM trades WHERE status!='OPEN'").fetchone()["s"]
    return cfg["balance_start_gbp"] + total


def today_closed(con, now):
    return con.execute("SELECT * FROM trades WHERE uk_day=? AND status!='OPEN' ORDER BY id",
                       (uk_day(now),)).fetchall()


def day_pnl(con, now):
    return sum(t["pnl_gbp"] for t in today_closed(con, now))


def losses_in_a_row(con, now):
    streak = 0
    for t in reversed(today_closed(con, now)):
        if t["pnl_gbp"] < 0:
            streak += 1
        else:
            break
    return streak


def open_trade(con):
    return con.execute("SELECT * FROM trades WHERE status='OPEN' LIMIT 1").fetchone()


def stopped_today(con, now):
    return get_state(con, "stopped_day") == uk_day(now)


# ---------------------------------------------------------------- SHIELD
def shield(card, cfg, con, now):
    def block(reason):
        return {"decision": "BLOCK", "reason": reason}

    if card.get("status") != "SETUP":
        return block("No valid setup")
    if not card.get("stop"):
        return block("No stop price")
    if stopped_today(con, now):
        return block("Desk stopped for today (" + str(get_state(con, "stopped_reason", "")) + ")")
    start = balance(con, cfg) - day_pnl(con, now)
    pnl = day_pnl(con, now)
    if pnl <= -start * cfg["daily_loss_limit_pct"] / 100:
        return block("Daily loss limit reached")
    if pnl >= start * cfg["daily_profit_target_pct"] / 100:
        return block("Daily profit target already hit")
    if losses_in_a_row(con, now) >= cfg["max_losses_in_a_row"]:
        return block(f"{cfg['max_losses_in_a_row']} losses in a row")
    open_count = con.execute("SELECT COUNT(*) c FROM trades WHERE status='OPEN'").fetchone()["c"]
    if open_count >= cfg["max_open_trades"]:
        return block("A position is already open")
    last_tick = get_state(con, "last_tick_ts")
    if last_tick is None or now.timestamp() - last_tick > cfg["feed_max_age_seconds"]:
        return block("Price feed is stale or missing")
    if not card.get("session"):
        return block("Outside allowed sessions")
    if GRADES[card["grade"]] < GRADES[cfg["min_grade"]]:
        return block(f"Grade {card['grade']} below minimum {cfg['min_grade']}")

    bal = balance(con, cfg)
    risk_gbp = bal * cfg["risk_per_trade_pct"] / 100
    lots_raw = (risk_gbp * card["gbpusd"]) / (card["risk_usd"] * cfg["oz_per_lot"])
    lots = int(lots_raw * 100) / 100  # round DOWN to 0.01
    if lots < 0.01:
        return block("Position size below 0.01 lots")
    if lots > cfg["max_lots"]:
        return block(f"Size {lots} lots is above max_lots {cfg['max_lots']} (stop too tight?)")
    approval_id = uuid.uuid4().hex
    con.execute("INSERT INTO approvals VALUES (?,?,0)", (approval_id, now.timestamp()))
    con.commit()
    real_risk = lots * card["risk_usd"] * cfg["oz_per_lot"] / card["gbpusd"]
    return {"decision": "APPROVE", "lots": lots, "risk_gbp": round(real_risk, 2),
            "approval_id": approval_id, "approved_at": now.timestamp()}


def stop_all(con, cfg, now, reason):
    set_state(con, "stopped_day", uk_day(now))
    set_state(con, "stopped_reason", reason)
    journal(con, "Shield", "STOP ALL", {"reason": reason}, now)
    send_telegram(f"STOP ALL - {reason}. No new trades until midnight UK.")


# ---------------------------------------------------------------- PILOT (paper)
def pilot(card, approval, cfg, con, now):
    if cfg.get("mode") != "PAPER" or cfg.get("live_trading"):
        return {"result": "REFUSED", "reason": "Live trading is not built yet - paper only"}
    row = con.execute("SELECT * FROM approvals WHERE approval_id=?", (approval.get("approval_id"),)).fetchone()
    if not row or row["used"]:
        return {"result": "REFUSED", "reason": "Approval missing or already used"}
    if now.timestamp() - row["ts"] > 30:
        return {"result": "REFUSED", "reason": "Approval older than 30 seconds"}
    con.execute("UPDATE approvals SET used=1 WHERE approval_id=?", (row["approval_id"],))
    direction = 1 if card["side"] == "BUY" else -1
    last = get_state(con, "last_price") or card["entry"]
    fill = last + direction * (cfg["spread_usd_estimate"] + cfg["slippage_usd_estimate"])
    cur = con.execute(
        "INSERT INTO trades(opened, uk_day, side, entry, stop, target, lots, risk_gbp, status, reason, gbpusd)"
        " VALUES (?,?,?,?,?,?,?,?, 'OPEN', ?, ?)",
        (now.isoformat(), uk_day(now), card["side"], round(fill, 2), card["stop"], card["target"],
         approval["lots"], approval["risk_gbp"], card["reason"], card["gbpusd"]))
    con.commit()
    return {"result": "FILLED (PAPER)", "trade_id": cur.lastrowid, "fill": round(fill, 2)}


def close_trade(con, cfg, trade, exit_price, why, now):
    direction = 1 if trade["side"] == "BUY" else -1
    pnl_usd = (exit_price - trade["entry"]) * direction * trade["lots"] * cfg["oz_per_lot"]
    pnl_gbp = round(pnl_usd / trade["gbpusd"], 2)
    r = round(pnl_gbp / trade["risk_gbp"], 2) if trade["risk_gbp"] else 0
    status = "WIN" if pnl_gbp > 0 else "LOSS" if pnl_gbp < 0 else "FLAT"
    con.execute("UPDATE trades SET status=?, closed=?, exit=?, pnl_gbp=?, r=? WHERE id=?",
                (status, now.isoformat(), round(exit_price, 2), pnl_gbp, r, trade["id"]))
    con.commit()
    journal(con, "Pilot", "CLOSE", {"id": trade["id"], "why": why, "exit": exit_price,
                                     "pnl_gbp": pnl_gbp, "r": r}, now)
    send_telegram(f"PAPER {trade['side']} closed ({why}) at {exit_price:.2f}: "
                  f"{'+' if pnl_gbp >= 0 else '-'}£{abs(pnl_gbp):.2f} ({r:+.2f}R). "
                  f"Day: £{day_pnl(con, now):+.2f}. Balance £{balance(con, cfg):,.2f}")
    # Shield checks after every close
    start = balance(con, cfg) - day_pnl(con, now)
    if losses_in_a_row(con, now) >= cfg["max_losses_in_a_row"]:
        stop_all(con, cfg, now, f"{cfg['max_losses_in_a_row']} losses in a row")
    elif day_pnl(con, now) <= -start * cfg["daily_loss_limit_pct"] / 100:
        stop_all(con, cfg, now, "daily loss limit hit")
    elif day_pnl(con, now) >= start * cfg["daily_profit_target_pct"] / 100:
        stop_all(con, cfg, now, f"+{cfg['daily_profit_target_pct']}% target banked")
    elif why == "target" and cfg.get("stop_after_target_win"):
        stop_all(con, cfg, now, "full 3R winner banked")
    return status, pnl_gbp


# ---------------------------------------------------------------- handlers
def handle_alert(alert, cfg, con, now):
    with LOCK:
        key = "|".join(str(alert.get(k, "")) for k in ("symbol", "side", "time", "price"))
        alert_id = hashlib.sha1(key.encode()).hexdigest()
        try:
            con.execute("INSERT INTO alerts VALUES (?,?)", (alert_id, now.isoformat()))
            con.commit()
        except sqlite3.IntegrityError:
            journal(con, "Scout", "DUPLICATE", {"alert_id": alert_id}, now)
            return {"scout": {"status": "DUPLICATE"}, "shield": None, "pilot": None}
        card = scout(alert, cfg, now)
        journal(con, "Scout", card["status"], card, now)
        result = {"scout": card, "shield": None, "pilot": None}
        if card["status"] != "SETUP":
            return result
        trade = open_trade(con)
        if trade and trade["side"] != card["side"] and cfg.get("exit_on_opposite_signal"):
            last = get_state(con, "last_price") or card["entry"]
            result["closed"] = close_trade(con, cfg, trade, last, "swing flipped", now)
        decision = shield(card, cfg, con, now)
        journal(con, "Shield", decision["decision"], decision, now)
        result["shield"] = decision
        if decision["decision"] == "APPROVE":
            fill = pilot(card, decision, cfg, con, now)
            journal(con, "Pilot", fill["result"], fill, now)
            result["pilot"] = fill
            send_telegram(f"PAPER {card['side']} XAUUSD {decision['lots']} lots @ {fill.get('fill')} | "
                          f"stop {card['stop']} | target {card['target'] or 'next flip'} | risk £{decision['risk_gbp']}\n"
                          f"{card['reason']}")
        elif card["grade"] == "A":
            send_telegram(f"A-grade {card['side']} setup BLOCKED by Shield: {decision['reason']}")
        return result


def handle_tick(tick, cfg, con, now):
    with LOCK:
        price = float(tick["price"])
        high, low = float(tick.get("high", price)), float(tick.get("low", price))
        set_state(con, "last_tick_ts", now.timestamp())
        set_state(con, "last_price", price)
        set_state(con, "feed_warned", False)
        day = uk_day(now)
        mkt = get_state(con, "market_day") or {}
        if mkt.get("day") != day:
            mkt = {"day": day, "open": price, "high": high, "low": low}
        mkt.update(high=max(mkt["high"], high), low=min(mkt["low"], low), last=price)
        set_state(con, "market_day", mkt)
        trade = open_trade(con)
        if not trade:
            return {"ok": True}
        slip = cfg["slippage_usd_estimate"]
        if trade["side"] == "BUY":
            if low <= trade["stop"]:  # stop first if both touched - worst case
                return {"closed": close_trade(con, cfg, trade, trade["stop"] - slip, "stop", now)}
            if trade["target"] is not None and high >= trade["target"]:
                return {"closed": close_trade(con, cfg, trade, trade["target"], "target", now)}
        else:
            if high >= trade["stop"]:
                return {"closed": close_trade(con, cfg, trade, trade["stop"] + slip, "stop", now)}
            if trade["target"] is not None and low <= trade["target"]:
                return {"closed": close_trade(con, cfg, trade, trade["target"], "target", now)}
        return {"ok": True}


def daily_report(cfg, con, now):
    day = uk_day(now)
    rows = con.execute("SELECT agent, event, detail FROM events WHERE ts LIKE ?", (day[:10] + "%",)).fetchall()
    grades = {"A": 0, "B": 0, "C": 0}
    blocks = []
    for r in rows:
        if r["agent"] == "Scout" and r["event"] == "SETUP":
            grades[json.loads(r["detail"])["grade"]] += 1
        if r["agent"] == "Shield" and r["event"] == "BLOCK":
            blocks.append(json.loads(r["detail"])["reason"])
    closed = today_closed(con, now)
    wins = sum(1 for t in closed if t["pnl_gbp"] > 0)
    mkt = get_state(con, "market_day") or {}
    market = (f"Gold opened {mkt['open']:.2f}, high {mkt['high']:.2f}, low {mkt['low']:.2f}, last {mkt['last']:.2f}."
              if mkt.get("day") == day else "No price data received today.")
    still_open = open_trade(con)
    decision = "No decision needed."
    if not rows:
        decision = "No alerts arrived today - check the TradingView alerts are switched on."
    elif get_state(con, "feed_warned"):
        decision = "The price feed went quiet - check the 1-minute TradingView alert."
    text = (f"BCB Gold Desk - {day} (PAPER)\n{market}\n"
            f"Scout: {sum(grades.values())} setups (A {grades['A']}, B {grades['B']}, C {grades['C']}).\n"
            f"Shield: {len(blocks)} blocked" + (f" - mainly: {max(set(blocks), key=blocks.count)}" if blocks else "") + ".\n"
            f"Pilot: {len(closed)} closed ({wins} won), {'1 open' if still_open else 'none open'}.\n"
            f"Day P&L £{day_pnl(con, now):+.2f}. Balance £{balance(con, cfg):,.2f}.\n"
            f"Decision: {decision}")
    return text


def watchdog_once(cfg, con, now):
    with LOCK:
        local = now.astimezone(UK)
        trade = open_trade(con)
        last = get_state(con, "last_price")
        if trade and last and local.hour * 60 + local.minute >= _minutes(cfg["flat_by"]):
            close_trade(con, cfg, trade, last, "flat by close", now)
        last_tick = get_state(con, "last_tick_ts")
        if in_session(cfg, now) and not get_state(con, "feed_warned"):
            since = max(last_tick or 0, STARTED)  # give a fresh start time to receive its first price
            if now.timestamp() - since > cfg["feed_dead_warn_seconds"]:
                set_state(con, "feed_warned", True)
                journal(con, "Shield", "FEED DEAD", {}, now)
                send_telegram("Price feed has gone quiet - Shield is blocking new trades until it's back.")
        if local.weekday() < 5 and local.strftime("%H:%M") >= cfg["daily_report_time"] \
                and get_state(con, "report_day") != uk_day(now):
            set_state(con, "report_day", uk_day(now))
            send_telegram(daily_report(cfg, con, now))


def watchdog_loop():
    con = db()
    while True:
        try:
            watchdog_once(load_config(), con, datetime.now(timezone.utc))
        except Exception as exc:
            print("[watchdog error]", exc)
        time.sleep(30)


# ---------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    con = None

    def _reply(self, code, body):
        data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"ok": True, "version": VERSION})
        if self.path == "/status":
            cfg = load_config()
            now = datetime.now(timezone.utc)
            with LOCK:
                t = open_trade(self.con)
            return self._reply(200, {"mode": cfg["mode"], "balance_gbp": balance(self.con, cfg),
                                     "day_pnl_gbp": day_pnl(self.con, now),
                                     "open_trade": dict(t) if t else None,
                                     "stopped_today": stopped_today(self.con, now)})
        if self.path.startswith("/events"):
            with LOCK:
                rows = self.con.execute(
                    "SELECT ts, agent, event, detail FROM events ORDER BY id DESC LIMIT 50").fetchall()
                last_tick = get_state(self.con, "last_tick_ts")
                last_price = get_state(self.con, "last_price")
            out = []
            for r in rows:
                d = json.loads(r["detail"] or "{}")
                why = d.get("reason") or d.get("why") or d.get("result") or ""
                uk = datetime.fromisoformat(r["ts"]).astimezone(UK).strftime("%a %H:%M")
                out.append(f"{uk}  {r['agent']}: {r['event']}  {why}".rstrip())
            age = (f"{int(time.time() - last_tick)}s ago at {last_price}" if last_tick else "never")
            text = (f"BCB Gold Desk {VERSION}\nLast price received: {age}\n"
                    f"Alerts/events (newest first):\n" + ("\n".join(out) or "none yet"))
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._reply(400, {"error": "body must be JSON"})
        secret = os.environ.get("WEBHOOK_SECRET")
        if not secret or payload.get("secret") != secret:
            journal(self.con, "Desk", "BAD SECRET", {"reason": f"{self.path} - secret in TradingView doesn't match Railway"},
                    datetime.now(timezone.utc))
            return self._reply(403, {"error": "bad secret"})
        cfg, now = load_config(), datetime.now(timezone.utc)
        try:
            if self.path == "/alert":
                return self._reply(200, handle_alert(payload, cfg, self.con, now))
            if self.path == "/tick":
                return self._reply(200, handle_tick(payload, cfg, self.con, now))
        except Exception as exc:
            journal(self.con, "Desk", "ERROR", {"path": self.path, "error": str(exc)}, now)
            send_telegram(f"Desk error on {self.path}: {exc}")
            return self._reply(500, {"error": str(exc)})
        self._reply(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def main():
    Handler.con = db()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    print(f"BCB Gold Desk {VERSION} listening on {port}")
    send_telegram(f"BCB Gold Desk {VERSION} started (PAPER).")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
