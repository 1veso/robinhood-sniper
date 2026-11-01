# book.py
# the desk's memory: positions.json and trades.csv. two files you can open yourself.
import csv, json, os, sys, time
import math, tempfile, uuid
from functools import wraps
from config import (POSITIONS, TRADES, PENDING, TICKET_USD, MAX_OPEN, MAX_ENTRIES_PER_DAY,
                    DAILY_LOSS_HALT_USD, COOLDOWN_HOURS)

FIELDS = ["time", "mint", "symbol", "action", "usd", "pnl", "reason"]


def _locked(fn):
    """Serialize ledger writes so a watch/pulse update cannot undo a confirmed fill."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with open(POSITIONS + ".lock", "a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return fn(*args, **kwargs)
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)
    return wrapped


def load() -> dict:
    if not os.path.exists(POSITIONS):
        return {}
    with open(POSITIONS) as f:
        return json.load(f)


def save(book: dict):
    # Readers see either the complete previous book or the complete new book.
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(os.path.abspath(POSITIONS)),
                                         prefix=".positions-", suffix=".tmp", delete=False) as f:
            path = f.name
            json.dump(book, f, indent=2)
        os.replace(path, POSITIONS)
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


@_locked
def update(mint: str, **fields):
    b = load()
    if mint in b:
        b[mint].update(fields)
        save(b)


def trades() -> list[dict]:
    if not os.path.exists(TRADES):
        return []
    with open(TRADES) as f:
        return list(csv.DictReader(f))


def record(mint, symbol, action, usd, pnl, reason):
    new = not os.path.exists(TRADES)
    with open(TRADES, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({"time": time.strftime("%Y-%m-%d %H:%M"), "mint": mint, "symbol": symbol,
                    "action": action, "usd": round(usd, 2), "pnl": round(pnl, 2), "reason": reason})


def today_pnl() -> float:
    day = time.strftime("%Y-%m-%d")
    return sum(float(t["pnl"]) for t in trades() if t["time"].startswith(day))


def guard(mint: str) -> str | None:
    """limits first. a blocked entry never touches the network."""
    day, book, log = time.strftime("%Y-%m-%d"), load(), trades()
    if today_pnl() <= -DAILY_LOSS_HALT_USD:                    return "halted_daily_loss"
    if mint in book:                                           return "already_open"
    if len(book) >= MAX_OPEN:                                  return "max_open"
    if sum(t["action"] == "open" and t["time"].startswith(day) for t in log) >= MAX_ENTRIES_PER_DAY:
        return "max_entries_today"
    cutoff = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - COOLDOWN_HOURS * 3600))
    if any(t["mint"] == mint and t["action"] == "close" and t["time"] >= cutoff for t in log):
        return "cooldown"
    return None


@_locked
def open_position(mint, symbol, price, liq_usd, snap: dict, usd=TICKET_USD):
    """snap = what the wallets looked like the moment you bought. watch.py compares against it."""
    b = load()
    b[mint] = {"symbol": symbol, "entry": price, "qty0": usd / price, "qty": usd / price,
               "peak": price, "liq0": liq_usd, "opened": time.time(), "took": False,
               "pulse": "unread", "last_pulse": 0, "trimmed_on_fade": False, **snap}
    save(b)
    record(mint, symbol, "open", usd, 0, "entry")


@_locked
def sell(mint, frac_of_original, price, reason) -> float:
    """sells a fraction of the ORIGINAL size (1.0 = everything left). returns realized pnl."""
    b = load()
    p = b[mint]
    q = min(p["qty"], p["qty0"] * frac_of_original)
    pnl = q * (price - p["entry"])
    p["qty"] -= q
    action = "close" if p["qty"] <= p["qty0"] * 1e-6 else "partial"
    record(mint, p["symbol"], action, q * price, pnl, reason)
    if action == "close":
        del b[mint]
    save(b)
    return pnl


@_locked
def request_exit(mint, frac_of_original, reason):
    """Live alert only: retain holdings and P&L until an explicit fill confirmation."""
    b = load()
    if mint not in b:
        return None
    p = b[mint]
    pending = p.get("pending_exit")
    # Keep the remaining quantity after a partial fill; never reset it each tick.
    if pending and pending["reason"] == reason:
        return pending
    filled = (p.get("exit_filled") or {}).get(reason, 0)
    pending = {"id": uuid.uuid4().hex, "reason": reason,
               "qty": min(p["qty"], p["qty0"] * frac_of_original - filled)}
    p["pending_exit"] = pending
    save(b)
    return pending


@_locked
def confirm_exit(mint, exit_id, actual_qty, fill_price):
    """Record one actual fill. Each alert ID can be confirmed only once."""
    if not math.isfinite(actual_qty) or actual_qty <= 0:
        raise ValueError("actual token quantity must be finite and positive")
    if not math.isfinite(fill_price) or fill_price <= 0:
        raise ValueError("actual fill price must be finite and positive")
    b = load()
    if mint not in b:
        raise ValueError("no open position; this fill may already have been confirmed")
    p = b[mint]
    pending = p.get("pending_exit")
    if not pending or pending["id"] != exit_id:
        raise ValueError("stale or already confirmed exit ID; use the current pending exit")
    if actual_qty > p["qty"]:
        raise ValueError("actual quantity exceeds the recorded holding; no sale recorded")
    pnl = actual_qty * (fill_price - p["entry"])
    p["qty"] -= actual_qty
    if pending["reason"] in ("take_profit", "x_fading"):
        filled = p.setdefault("exit_filled", {})
        filled[pending["reason"]] = filled.get(pending["reason"], 0) + actual_qty
    complete = actual_qty >= pending["qty"]
    if complete:
        p.pop("pending_exit")
        if pending["reason"] == "take_profit":
            p["took"] = True
        if pending["reason"] == "x_fading":
            p["trimmed_on_fade"] = True
    else:
        pending["qty"] -= actual_qty
        pending["id"] = uuid.uuid4().hex  # replaying this confirmation cannot sell twice
    action = "close" if p["qty"] <= p["qty0"] * 1e-6 else "partial"
    record(mint, p["symbol"], action, actual_qty * fill_price, pnl, pending["reason"])
    if action == "close":
        del b[mint]
    save(b)
    return pnl


if __name__ == "__main__":
    # live mode only. after you buy in your own wallet:  python book.py add <mint> <usd> <fill_price>
    # after you sell by hand:                            python book.py close <mint> <price>
    # confirm an alerted fill: python book.py confirm <mint> <exit_id> <actual_token_qty> <fill_price>
    cmd, mint = sys.argv[1], sys.argv[2]
    if cmd == "add":
        pend = json.load(open(PENDING)) if os.path.exists(PENDING) else {}
        if mint not in pend:
            sys.exit("no snapshot for this mint. run enter.py on it first.")
        s = pend.pop(mint)
        open_position(mint, s["symbol"], float(sys.argv[4]), s["liq_usd"], s["snap"], float(sys.argv[3]))
        json.dump(pend, open(PENDING, "w"), indent=2)
        print(f"{s['symbol']} is in the book. watch.py guards it from the next run.")
    elif cmd == "close":
        print(round(sell(mint, 1.0, float(sys.argv[3]), "manual"), 2))
    elif cmd == "confirm":
        if len(sys.argv) != 6:
            sys.exit("usage: python book.py confirm <mint> <exit_id> <actual_token_qty> <fill_price>")
        try:
            pnl = confirm_exit(mint, sys.argv[3], float(sys.argv[4]), float(sys.argv[5]))
        except ValueError as e:
            sys.exit(str(e))
        print(f"Confirmed fill recorded. Realized pnl ${pnl:+.2f}")
        p = load().get(mint)
        if p and p.get("pending_exit"):
            pending = p["pending_exit"]
            print(f"Still pending: {pending['qty']:.12g} tokens. After the next actual fill: "
                  f"python book.py confirm {mint} {pending['id']} <actual_token_qty> <fill_price>")
