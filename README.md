# BCB Gold Desk v1.0.0 (paper)

Scout, Shield, paper Pilot, Journal and Telegram reports for XAU/USD. Runs on Railway.
Settings live in `config.yaml`. No real money: Pilot refuses anything but PAPER.

## Files
| File | What it is |
| --- | --- |
| desk.py | The desk itself (Scout, Shield, Pilot, Journal, daily report) |
| config.yaml | Your rules in plain English |
| stress_test.py | 17 failure tests. Run `python stress_test.py` |
| Procfile, railway.json, requirements.txt | Tell Railway how to run it and restart it after a crash |
| bcb_desk_price_feed.pine | TradingView indicator that sends 1-minute prices |
| bcb_desk_signal_snippet.pine | The alert block that goes inside FX Trader |

## Set-up (phone friendly)
1. GitHub: new repo `bcb-gold-desk`, upload every file here.
2. Railway: New Project -> Deploy from GitHub -> `bcb-gold-desk`.
3. Railway -> Variables:
   - `WEBHOOK_SECRET` = a long random word (also typed into both TradingView indicators)
   - `TELEGRAM_BOT_TOKEN` = BCB Scanner bot token (same as Gem Hunter)
   - `TELEGRAM_CHAT_ID` = your chat ID (same as Gem Hunter)
   - `DESK_DB` = `/data/desk.db`
4. Railway -> the service -> Volumes -> Add volume, mount path `/data` (keeps the ledger through redeploys).
5. Railway -> Settings -> Networking -> Generate domain. Open `https://<domain>/health` and it should say ok.
6. TradingView, XAUUSD 1-minute chart: add `BCB Desk Price Feed`, enter the secret, create alert
   "Any alert() function call", webhook `https://<domain>/tick`, open-ended.
7. TradingView, XAUUSD 15m chart: FX Trader with the signal snippet merged in, alert
   "Any alert() function call", webhook `https://<domain>/alert`.

Check any time: `https://<domain>/status`.
