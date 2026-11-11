#!/usr/bin/env python3
"""Bot de simulation ORB - compte PAPIER Alpaca uniquement.

Lance toutes les 5 minutes par le Planificateur de taches Windows :
  uv run --python 3.12 bot.py
Bibliotheque standard seulement. Cles dans .env (meme dossier) :
  ALPACA_API_KEY=...
  ALPACA_SECRET_KEY=...

Regles (reprises de day-trader-skill, config.json) :
- range d'ouverture = 9:30-9:45 ET (3 bougies de 5 min)
- entree : premiere bougie de 5 min CLOTUREE au-dessus du haut du range, entre 9:45 et 15:30 ET
- stop = milieu du range, objectif = 1R, ordre bracket
- taille = perte max par trade / risque, plafonnee ; max trades/jour, perte max/jour, capital simule
- a partir de 15:50 ET : tout est ferme
"""
import json, math, os, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TRADE = "https://paper-api.alpaca.markets/v2"   # PAPIER UNIQUEMENT, ne jamais changer
DATA = "https://data.alpaca.markets/v2"


def now_paris():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M")
    except Exception:
        return datetime.now().strftime("%H:%M")


def log(msg):
    line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    with open(os.path.join(HERE, "bot.log"), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def journal(row):
    path = os.path.join(HERE, "journal.csv")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("date;heure_paris;action;symbole;qte;prix;stop;objectif;note\n")
        fh.write(row + "\n")


def load_env():
    env = {}
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for ln in open(p, encoding="utf-8-sig"):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    key = env.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY")
    sec = env.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")
    if not key or not sec:
        log("ERREUR : cles absentes du fichier .env")
        sys.exit(1)
    return key, sec


KEY = SEC = None


def api(method, url, params=None, body=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC,
        "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s %s : %s" % (e.code, url.split("?")[0], e.read()[:300].decode("utf-8", "replace")))


def parse_ts(s):
    # "2026-11-11T14:30:00Z" ou "2026-11-11T09:30:00.123-05:00"
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        tz = tail[-6:] if tail[-6] in "+-" else ""
        s = head + tz
    return datetime.fromisoformat(s)


def state_load():
    p = os.path.join(HERE, "state.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def state_save(st):
    json.dump(st, open(os.path.join(HERE, "state.json"), "w", encoding="utf-8"))


def flat_orders(orders):
    out = []
    for o in orders or []:
        out.append(o)
        out.extend(o.get("legs") or [])
    return out


def main():
    global KEY, SEC
    KEY, SEC = load_env()
    c = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))

    acct = api("GET", TRADE + "/account")
    if not str(acct.get("account_number", "")).startswith("PA"):
        log("ALERTE : compte non papier (%s). Arret." % acct.get("account_number"))
        journal("%s;%s;ALERTE;;;;;;compte non papier, aucun ordre" % (datetime.now().date(), now_paris()))
        return

    clock = api("GET", TRADE + "/clock")
    et = parse_ts(clock["timestamp"])          # heure de New York avec son decalage
    off = et.utcoffset()
    today = et.date().isoformat()
    st = state_load()
    if st.get("date") != today:
        st = {"date": today}

    # ---- bilan apres la cloture (une fois) ----
    if not clock["is_open"]:
        pos = api("GET", TRADE + "/positions")
        if pos and not st.get("overnight_close"):
            api("DELETE", TRADE + "/positions", {"cancel_orders": "true"})
            log("SECURITE : %d position(s) encore ouverte(s) marche ferme -> fermeture demandee" % len(pos))
            journal("%s;%s;ALERTE;;;;;;positions restees ouvertes apres la cloture, fermeture demandee" % (today, now_paris()))
            st["overnight_close"] = True; st["closed"] = True; state_save(st)
        if st.get("closed") and not st.get("summarized") and et.hour >= 15:
            summarize(c, today, off, st)
        return

    hm = et.hour * 60 + et.minute
    H = lambda s: int(s.split(":")[0]) * 60 + int(s.split(":")[1])

    # ---- cloture de fin de journee ----
    if hm >= H(c["flat_by"]):
        if not st.get("closed"):
            pos = api("GET", TRADE + "/positions")
            if pos:
                api("DELETE", TRADE + "/positions", {"cancel_orders": "true"})
                log("Cloture : %d position(s) fermee(s)" % len(pos))
            else:
                api("DELETE", TRADE + "/orders")
            st["closed"] = True
            state_save(st)
        elif not st.get("summarized") and hm >= H(c["flat_by"]) + 4:
            summarize(c, today, off, st)
        return

    if hm < H(c["no_entries_before"]) or hm > H(c["no_entries_after"]):
        return

    # ---- etat du jour ----
    equity, last_eq = float(acct["equity"]), float(acct["last_equity"])
    pnl_today = equity - last_eq
    if pnl_today <= -c["max_daily_loss_usd"]:
        if not st.get("daily_stop"):
            journal("%s;%s;RIEN;;;;;;perte max du jour atteinte (%.2f USD)" % (today, now_paris(), pnl_today))
            st["daily_stop"] = True; state_save(st)
        return
    start_day = datetime.fromisoformat(today + "T09:00:00").replace(tzinfo=et.tzinfo)
    orders = api("GET", TRADE + "/orders", {"status": "all", "after": start_day.isoformat(), "limit": 500, "nested": "true"})
    prefix = "sim-%s-" % today.replace("-", "")
    traded = set()
    for o in orders or []:
        if o.get("side") == "buy" and (o.get("client_order_id", "").startswith(prefix) or o.get("status") == "filled"):
            if o.get("status") not in ("canceled", "rejected", "expired"):
                traded.add(o["symbol"])
    positions = api("GET", TRADE + "/positions") or []
    held = {p["symbol"] for p in positions}
    open_notional = sum(abs(float(p["market_value"])) for p in positions)
    slots = c["max_trades_per_day"] - len(traded)
    if slots <= 0:
        return

    # ---- donnees : bougies de 5 min depuis 9:30 ET ----
    open_et = datetime.fromisoformat(today + "T09:30:00").replace(tzinfo=et.tzinfo)
    syms = c["watchlist"]
    bars, token = {}, None
    while True:
        p = {"symbols": syms, "timeframe": "5Min", "start": open_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "feed": c.get("feed", "iex"), "limit": 10000}
        if token:
            p["page_token"] = token
        d = api("GET", DATA + "/stocks/bars", p)
        for s, bl in (d.get("bars") or {}).items():
            bars.setdefault(s, []).extend(bl)
        token = d.get("next_page_token")
        if not token:
            break

    cands = []
    for sym, bl in bars.items():
        if sym in traded or sym in held:
            continue
        bl = [dict(b, _t=parse_ts(b["t"]).astimezone(et.tzinfo)) for b in bl]
        orb = [b for b in bl if b["_t"] < open_et + timedelta(minutes=15)]
        done = [b for b in bl if b["_t"] >= open_et + timedelta(minutes=15) and b["_t"] + timedelta(minutes=5) <= et]
        if len(orb) < 2 or not done:
            continue
        hi = max(float(b["h"]) for b in orb); lo = min(float(b["l"]) for b in orb)
        mid = (hi + lo) / 2
        last = done[-1]; px = float(last["c"])
        # premiere cloture au-dessus du range dans les N dernieres bougies, et le cours y est toujours
        first = next((i for i, b in enumerate(done) if float(b["c"]) > hi), None)
        if first is None or px <= hi or first < len(done) - c.get("max_breakout_age_bars", 1):
            continue
        if px > hi + c.get("max_extension_R", 1.0) * (hi - (hi + lo) / 2):
            continue
        if px < c["min_price"] or (hi - lo) / px > c["max_range_pct"] / 100:
            continue
        risk = px - mid
        if risk / px < c["min_risk_pct"] / 100:
            continue
        target = px + c["target_R"] * risk
        qty = min(math.floor(c["max_loss_per_trade_usd"] / risk), math.floor(c["max_position_usd"] / px))
        if qty < 1:
            continue
        cands.append((sym, qty, px, hi, lo, round(mid, 2), round(target, 2)))

    cash_cap = c["capital_usd"] * (1 - c["cash_buffer_pct"] / 100) - open_notional
    for sym, qty, px, hi, lo, stop, target in cands:
        if slots <= 0:
            break
        if qty * px > cash_cap:
            qty = math.floor(cash_cap / px)
            if qty < 1:
                continue
            log("%s : taille reduite a %d (capital simule restant)" % (sym, qty))
        body = {"symbol": sym, "qty": str(qty), "side": "buy", "type": "market", "time_in_force": "day",
                "order_class": "bracket", "take_profit": {"limit_price": "%.2f" % target},
                "stop_loss": {"stop_price": "%.2f" % stop}, "client_order_id": prefix + sym}
        try:
            r = api("POST", TRADE + "/orders", body=body)
            log("ACHAT %s x%d ~%.2f stop %.2f obj %.2f -> %s" % (sym, qty, px, stop, target, r.get("status")))
            journal("%s;%s;ACHAT;%s;%d;%.2f;%.2f;%.2f;cassure du range %.2f-%.2f (bot)" % (today, now_paris(), sym, qty, px, stop, target, lo, hi))
            slots -= 1; cash_cap -= qty * px
        except RuntimeError as e:
            log("ordre %s rejete : %s" % (sym, e))
            journal("%s;%s;REJET;%s;%d;%.2f;%.2f;%.2f;%s" % (today, now_paris(), sym, qty, px, stop, target, str(e)[:120]))


def summarize(c, today, off, st):
    start_day = datetime.fromisoformat(today + "T09:00:00").replace(tzinfo=timezone(off))
    orders = api("GET", TRADE + "/orders", {"status": "all", "after": start_day.isoformat(), "limit": 500, "nested": "true"})
    per = {}
    for o in flat_orders(orders):
        if o.get("status") != "filled" and float(o.get("filled_qty") or 0) == 0:
            continue
        q = float(o.get("filled_qty") or 0); p = float(o.get("filled_avg_price") or 0)
        d = per.setdefault(o["symbol"], {"buy": 0.0, "sell": 0.0, "qty": 0.0, "out": 0.0, "how": "cloture"})
        if o["side"] == "buy":
            d["buy"] += q * p; d["qty"] += q
        else:
            d["sell"] += q * p; d["out"] += q
            role = o.get("group_role") or ""
            d["how"] = {"take_profit": "objectif", "stop_loss": "stop"}.get(role, "cloture")
    total = 0.0
    for sym, d in per.items():
        if d["out"] <= 0:
            continue
        pl = d["sell"] - d["buy"]; total += pl
        journal("%s;%s;VENTE;%s;%d;%.2f;;;%s P&L=%.2f USD (%.2f EUR)" % (today, now_paris(), sym, d["out"], d["sell"] / d["out"], d["how"], pl, pl / c["eurusd"]))
    acct = api("GET", TRADE + "/account")
    cum = float(acct["equity"]) - c["alpaca_start_equity"]
    journal("%s;%s;BILAN;;;;;;jour %.2f EUR | cumul %.2f EUR | capital simule %.2f EUR" % (
        today, now_paris(), total / c["eurusd"], cum / c["eurusd"], (c["capital_usd"] + cum) / c["eurusd"]))
    log("Bilan ecrit : jour %.2f USD, cumul %.2f USD" % (total, cum))
    st["summarized"] = True
    state_save(st)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("ERREUR : %r" % e)
        sys.exit(1)
