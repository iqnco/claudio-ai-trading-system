"""Orders and reconciliation against Schwab.

Rules this module enforces no matter what the caller asks:
  * long only: never sells more than the account holds
  * every buy goes in as ONE order with its protective stop attached
    (first-triggers-second): the GTC stop activates the instant the buy fills
  * stops only move up
  * DRY mode (default) logs every order instead of sending it
"""
import itertools
import time
from datetime import datetime, timedelta, timezone

from . import data, db
from . import rules as R

WORKING = {"WORKING", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION", "AWAITING_PARENT_ORDER",
           "AWAITING_CONDITION", "AWAITING_STOP_CONDITION", "AWAITING_MANUAL_REVIEW",
           "AWAITING_RELEASE_TIME", "PENDING_ACKNOWLEDGEMENT", "NEW", "PENDING_REPLACE",
           "PENDING_CANCEL", "DRY"}
DONE_BAD = {"CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
_dry_ids = itertools.count(1)


def live_enabled():
    from schwab_api import config
    return bool(config.TRADING_ENABLED)


def _px(x):
    return f"{x:.2f}" if x >= 1 else f"{x:.4f}"


def _place(acct_hash, spec, kind, symbol, side, qty, price=None, stop=None, **meta):
    """Send (or dry-log) an order. Returns order id string."""
    if not live_enabled():
        oid = f"DRY-{int(time.time())}-{next(_dry_ids)}"
        db.record_order(oid, symbol, kind, side, qty, price, stop, status="DRY",
                        spec=spec.build(), **meta)
        db.log(f"DRY {kind}", symbol, side=side, qty=qty, price=price, stop=stop, **meta)
        return oid
    from schwab.utils import Utils
    c = data.client()
    r = c.place_order(acct_hash, spec)
    if r.status_code not in (200, 201):
        db.log("order_rejected", symbol, kind=kind, status=r.status_code, body=r.text[:300])
        raise RuntimeError(f"{kind} {symbol} rejected: {r.status_code} {r.text[:200]}")
    oid = str(Utils(c, acct_hash).extract_order_id(r))
    db.record_order(oid, symbol, kind, side, qty, price, stop, **meta)
    db.log(f"placed {kind}", symbol, order_id=oid, side=side, qty=qty, price=price, stop=stop, **meta)
    return oid


def _stop_spec(symbol, qty, stop):
    from schwab.orders.common import (Duration, EquityInstruction, OrderStrategyType,
                                      OrderType, Session)
    from schwab.orders.generic import OrderBuilder
    return (OrderBuilder().set_order_type(OrderType.STOP).set_session(Session.NORMAL)
            .set_duration(Duration.GOOD_TILL_CANCEL).set_stop_price(_px(stop))
            .set_order_strategy_type(OrderStrategyType.SINGLE)
            .add_equity_leg(EquityInstruction.SELL, symbol, int(qty)))


def buy_with_stop(acct_hash, symbol, qty, limit, stop, **meta):
    """Limit buy (DAY) that triggers a GTC stop-market sell for the same qty."""
    from schwab.orders.common import Duration, Session, first_triggers_second
    from schwab.orders.equities import equity_buy_limit
    assert stop < limit and qty >= 1
    buy = (equity_buy_limit(symbol, int(qty), _px(limit))
           .set_duration(Duration.DAY).set_session(Session.NORMAL))
    spec = first_triggers_second(buy, _stop_spec(symbol, qty, stop))
    return _place(acct_hash, spec, "entry", symbol, "BUY", qty, limit, stop, **meta)


def place_stop(acct_hash, symbol, qty, stop, **meta):
    return _place(acct_hash, _stop_spec(symbol, qty, stop), "stop", symbol, "SELL", qty,
                  stop=stop, **meta)


def cancel(acct_hash, order_id, symbol=None):
    if str(order_id).startswith("DRY") or not live_enabled():
        db.set_order_status(order_id, "CANCELED")
        db.log("DRY cancel", symbol, order_id=order_id)
        return True
    r = data.client().cancel_order(order_id, acct_hash)
    ok = r.status_code in (200, 201)
    db.log("cancel" if ok else "cancel_failed", symbol, order_id=order_id, status=r.status_code)
    if ok:
        db.set_order_status(order_id, "CANCELED")
    return ok


def replace_stop(acct_hash, old_id, symbol, qty, new_stop, **meta):
    """Cancel/replace a stop. Returns the new order id (Schwab issues a new one)."""
    spec = _stop_spec(symbol, qty, new_stop)
    if str(old_id).startswith("DRY") or not live_enabled():
        db.set_order_status(old_id, "REPLACED")
        return _place(acct_hash, spec, "stop", symbol, "SELL", qty, stop=new_stop, **meta)
    from schwab.utils import Utils
    c = data.client()
    r = c.replace_order(acct_hash, old_id, spec)
    if r.status_code not in (200, 201):
        db.log("replace_failed", symbol, order_id=old_id, status=r.status_code, body=r.text[:300])
        raise RuntimeError(f"replace stop {symbol} failed: {r.status_code}")
    new_id = str(Utils(c, acct_hash).extract_order_id(r))
    db.set_order_status(old_id, "REPLACED")
    db.record_order(new_id, symbol, "stop", "SELL", qty, stop=new_stop, **meta)
    db.log("stop raised", symbol, old=old_id, new=new_id, stop=new_stop, qty=qty)
    return new_id


def sell_market(acct_hash, symbol, qty, reason, held_qty):
    """Market sell during the regular session. Refuses to sell more than is held."""
    from schwab.orders.common import Duration, Session
    from schwab.orders.equities import equity_sell_market
    qty = int(min(qty, held_qty))
    if qty < 1:
        raise ValueError(f"nothing to sell for {symbol}")
    spec = equity_sell_market(symbol, qty).set_duration(Duration.DAY).set_session(Session.NORMAL)
    return _place(acct_hash, spec, "exit", symbol, "SELL", qty, reason=reason)


# ── reconciliation ───────────────────────────────────────────────────────
def _flatten(orders):
    out = {}
    for o in orders or []:
        out[str(o["orderId"])] = o
        for ch in o.get("childOrderStrategies", []) or []:
            ch = dict(ch)
            ch["_parent"] = str(o["orderId"])
            out.update(_flatten([ch]))
    return out


def fill_price(o):
    legs = [leg for act in o.get("orderActivityCollection", []) or []
            for leg in act.get("executionLegs", []) or []]
    qty = sum(leg.get("quantity", 0) for leg in legs)
    if not qty:
        return None
    return sum(leg["price"] * leg.get("quantity", 0) for leg in legs) / qty


def order_map(acct_hash, extra_ids=()):
    """Every order touched in the last 60 days, children included, keyed by id."""
    m = _flatten(data.orders(acct_hash, days=60))
    for oid in extra_ids:
        if oid and not str(oid).startswith("DRY") and str(oid) not in m:
            try:
                m.update(_flatten([data.order(acct_hash, oid)]))
            except Exception:
                pass
    return m


def is_working(order_id, omap):
    if not order_id:
        return False
    if str(order_id).startswith("DRY"):
        return True
    o = omap.get(str(order_id))
    return bool(o) and o.get("status") in WORKING


def stale_entry(o, now=None):
    created = datetime.fromisoformat(o["created"])
    now = now or datetime.now(timezone.utc)
    return now - created > timedelta(minutes=R.ENTRY_ORDER_TTL_MIN)
