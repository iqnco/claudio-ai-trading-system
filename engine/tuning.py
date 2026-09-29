"""Parameters the weekly strategist may propose changing, with hard bounds.

Overrides live in the database (state key "overrides") and are applied on top of
rules.py defaults. Only the owner applies them (/approve in Telegram). Anything not
listed here (kill switches, risk caps, position caps) can never be tuned by the AI.
RISK_PER_TRADE can only go down from the owner's 1.5%.
"""
import copy

from . import db
from . import rules as R

TUNABLE = {
    "SETUPS_ENABLED.momentum": (bool, None, None),
    "SETUPS_ENABLED.bounce": (bool, None, None),
    "SETUPS_ENABLED.catalyst": (bool, None, None),
    "SETUPS_ENABLED.value": (bool, None, None),
    "RISK_PER_TRADE": (float, 0.005, 0.015),
    "SETUP_SIZE_MULT.momentum": (float, 0.25, 1.0),
    "SETUP_SIZE_MULT.bounce": (float, 0.25, 1.0),
    "SETUP_SIZE_MULT.catalyst": (float, 0.25, 1.0),
    "SETUP_SIZE_MULT.value": (float, 0.25, 1.0),
    "BREAKEVEN_AT_R": (float, 0.75, 1.5),
    "PARTIAL_AT_R": (float, 1.5, 3.0),
    "MOMENTUM.vol_mult": (float, 1.2, 2.5),
    "MOMENTUM.max_stop_pct": (float, 0.06, 0.12),
    "MOMENTUM.time_stop_days": (int, 4, 10),
    "MOMENTUM.max_hold_days": (int, 8, 25),
    "BOUNCE.rsi2_max": (float, 3.0, 20.0),
    "BOUNCE.drop_from_10d_high": (float, 0.05, 0.15),
    "BOUNCE.max_hold_days": (int, 3, 8),
    "CATALYST.min_gap": (float, 0.03, 0.10),
    "CATALYST.vol_mult": (float, 2.0, 5.0),
    "CATALYST.time_stop_days": (int, 4, 10),
    "VALUE.max_pe_vs_sector": (float, 0.5, 0.95),
    "VALUE.min_rev_growth": (float, 0.0, 20.0),
    "VALUE.max_stop_pct": (float, 0.08, 0.15),
}

_DEFAULTS = {}


def _split(key):
    return key.split(".", 1) if "." in key else (key, None)


def get(key):
    top, sub = _split(key)
    obj = getattr(R, top)
    return obj[sub] if sub else obj


def _set(key, value):
    top, sub = _split(key)
    if sub:
        getattr(R, top)[sub] = value
    else:
        setattr(R, top, value)


def snapshot_defaults():
    if not _DEFAULTS:
        for k in TUNABLE:
            _DEFAULTS[k] = copy.deepcopy(get(k))


def validate(key, value):
    """Returns the coerced value, or raises ValueError."""
    if key not in TUNABLE:
        raise ValueError(f"{key} is not tunable")
    typ, lo, hi = TUNABLE[key]
    if typ is bool:
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "on", "1", "yes", "enable", "enabled")
        return bool(value)
    v = typ(value)
    if v < lo or v > hi:
        raise ValueError(f"{key}={v} outside allowed range {lo}–{hi}")
    return v


def load():
    """Reset tunables to defaults, then apply stored overrides."""
    snapshot_defaults()
    for k, v in _DEFAULTS.items():
        _set(k, copy.deepcopy(v))
    for k, v in (db.get_state("overrides", {}) or {}).items():
        try:
            _set(k, validate(k, v))
        except ValueError as e:
            db.log("override_invalid", None, key=k, value=v, error=str(e))


def apply(key, value, source="owner"):
    v = validate(key, value)
    ov = db.get_state("overrides", {}) or {}
    old = get(key)
    ov[key] = v
    db.set_state("overrides", ov)
    load()
    db.log("tuning applied", None, key=key, old=old, new=v, source=source)
    return old, v


def current():
    snapshot_defaults()
    return {k: dict(value=get(k), default=_DEFAULTS[k],
                    range=None if TUNABLE[k][0] is bool else [TUNABLE[k][1], TUNABLE[k][2]])
            for k in TUNABLE}
