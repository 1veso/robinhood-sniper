# watch.py
# every open position, every 20 seconds. wallets first, pool second, price last.
# silent when nothing moved. run: python watch.py --minutes 9 --every 20
import argparse, time
from config import WATCH, PULSE, EXIT, PAPER
import book, facts, rpc


def tick() -> list[str]:
    events = []
    for mint, p in list(book.load().items()):
        try:
            m = facts.pool(mint)
        except Exception as e:
            events.append(f"{p['symbol']}: dexscreener down ({e}), holding, next tick retries")
            continue
        if not m or m["price"] <= 0:
            if PAPER:
                book.sell(mint, 1.0, 0.0, "pool_gone")
                events.append(f"{p['symbol']}: POOL GONE. treat as rugged, sell whatever you can")
            else:
                pending = book.request_exit(mint, 1.0, "pool_gone")
                if pending:
                    events.append(f"{p['symbol']}: POOL GONE. sell whatever you can; no sale recorded. "
                                  f"After an actual fill: python book.py confirm {mint} {pending['id']} "
                                  f"<actual_token_qty> <fill_price>")
            continue

        price = m["price"]
        p["peak"] = max(p["peak"], price)
        book.update(mint, peak=p["peak"])
        pct = (price / p["entry"] - 1) * 100

        def out(frac, why, detail=""):
            if PAPER:
                pnl = book.sell(mint, frac, price, why)
            else:
                pending = book.request_exit(mint, frac, why)
                if pending:
                    events.append(f"{p['symbol']}: {why.upper()} {detail}-> SELL NOW in your wallet: "
                                  f"{pending['qty']:.12g} tokens at {pct:+.0f}% quoted; no sale recorded. "
                                  f"After an actual fill: python book.py confirm {mint} {pending['id']} "
                                  f"<actual_token_qty> <fill_price>")
                return
            verb = "sold"
            size = "all" if frac >= 1 else f"{int(frac * 100)}%"
            events.append(f"{p['symbol']}: {why.upper()} {detail}-> {verb} {size} at {pct:+.0f}%, pnl ${pnl:+.2f}")

        # 1. the dev wallet. armed only if the dev held real size at entry
        if p["dev0_pct"] >= WATCH["dev_arm_pct"]:
            try:
                dev = rpc.wallet_balance(p["creator"], mint)
                drop = (1 - dev / p["dev0"]) * 100 if p["dev0"] else 0
                if drop >= WATCH["dev_sell_pct"]:
                    out(1.0, "dev_sold", f"(dev wallet -{drop:.0f}%) "); continue
            except Exception as e:
                events.append(f"{p['symbol']}: rpc down ({e}), dev check skipped this tick")

        # 2. the entry top 10. one call for all ten accounts
        if p["holders0"]:
            try:
                now = rpc.balances(list(p["holders0"]))
                before, after = sum(p["holders0"].values()), sum(now.values())
                gone = sum(1 for a, v in now.items() if v == 0 and p["holders0"][a] > 0)
                peel = (1 - after / before) * 100 if before else 0
                if peel >= WATCH["holders_peel_pct"] or gone >= WATCH["holders_gone"]:
                    out(1.0, "holders_exit", f"(top 10 -{peel:.0f}%, {gone} emptied) "); continue
            except Exception as e:
                events.append(f"{p['symbol']}: rpc down ({e}), holder check skipped this tick")

        # 3. the pool itself
        if m["liq_usd"] < p["liq0"] * (1 - WATCH["liq_drop_pct"] / 100):
            out(1.0, "liquidity_pulled"); continue

        # 4. X, as last read by pulse.py
        if p["pulse"] == "dead":
            out(1.0, "x_dead"); continue
        if p["pulse"] == "fading" and not p["trimmed_on_fade"]:
            out(PULSE["fading_sells"], "x_fading")
            if PAPER:
                book.update(mint, trimmed_on_fade=True)
            continue

        # 5. price. the last and dumbest line of defense
        hours = (time.time() - p["opened"]) / 3600
        if pct <= EXIT["stop_pct"]:
            out(1.0, "stop"); continue
        if not p["took"] and pct >= EXIT["take_pct"]:
            out(EXIT["take_frac"], "take_profit")
            if PAPER:
                book.update(mint, took=True)
            continue
        if p["took"] and price <= p["peak"] * (1 - EXIT["trail_pct"] / 100):
            out(1.0, "trailing_stop"); continue
        if hours >= EXIT["time_stop_hours"] and pct < EXIT["time_stop_min_pct"]:
            out(1.0, "time_stop")
    return events


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=9)
    ap.add_argument("--every", type=int, default=20)
    a = ap.parse_args()
    end = time.time() + a.minutes * 60
    while True:
        for e in tick():
            print(time.strftime("%H:%M:%S"), e, flush=True)
        if time.time() + a.every > end or not book.load():
            break
        time.sleep(a.every)
