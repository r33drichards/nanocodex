"""Tiny 5-field crontab parser (dependency-free, matching the repo's
no-extra-deps ethos — cf. the sh MCP server and the TOML fallback emitter).

`parse(expr).next_after(dt)` returns the next datetime (minute resolution,
naive local time) the expression matches strictly after `dt`.

Supported syntax: `*`, numbers, lists `a,b`, ranges `a-b`, steps `*/n` and
`a-b/n`, month names (jan..dec), day names (sun..sat), day-of-week 0-7 (0 and
7 are both Sunday), and the @hourly/@daily/@midnight/@weekly/@monthly/
@yearly/@annually aliases. Day matching follows vixie cron: when BOTH
day-of-month and day-of-week are restricted, a day matching EITHER fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date, datetime, time as _time, timedelta

ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

_MONTHS = {n: i + 1 for i, n in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_DAYS = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}


def _atom(token: str, lo: int, hi: int, names: dict[str, int]) -> int:
    t = token.strip().lower()
    if t in names:
        return names[t]
    try:
        v = int(t)
    except ValueError:
        raise ValueError(f"bad cron field value {token!r}")
    return v


def _field(spec: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    """One cron field -> the set of matching values in [lo, hi]."""
    names = names or {}
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty item in cron field {spec!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"bad cron step {step_s!r}")
            if step < 1:
                raise ValueError(f"bad cron step {step}")
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = _atom(a_s, lo, hi, names), _atom(b_s, lo, hi, names)
        else:
            a = b = _atom(part, lo, hi, names)
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"cron field value out of range in {spec!r} (allowed {lo}-{hi})")
        out.update(range(a, b + 1, step))
    return out


@dataclass(frozen=True)
class CronExpr:
    minutes: frozenset[int]
    hours: frozenset[int]
    dom: frozenset[int]
    months: frozenset[int]
    dow: frozenset[int]
    dom_star: bool
    dow_star: bool

    def _day_matches(self, day: _date) -> bool:
        if day.month not in self.months:
            return False
        dom_ok = day.day in self.dom
        dow_ok = ((day.weekday() + 1) % 7) in self.dow  # python Mon=0 -> cron Sun=0
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok  # both restricted: vixie OR

    def next_after(self, after: datetime) -> datetime:
        """Next matching datetime strictly after `after` (minute resolution)."""
        t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        day = t.date()
        # 5 years covers the sparsest valid expression (Feb 29).
        for i in range(366 * 5):
            if self._day_matches(day):
                h0, m0 = (t.hour, t.minute) if i == 0 else (0, 0)
                for h in hours:
                    if h < h0:
                        continue
                    for m in minutes:
                        if h > h0 or m >= m0:
                            return datetime.combine(day, _time(hour=h, minute=m))
            day += timedelta(days=1)
        raise ValueError("cron expression never matches (within 5 years)")


def parse(expr: str) -> CronExpr:
    spec = ALIASES.get(expr.strip().lower(), expr.strip())
    fields = spec.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have 5 fields (minute hour day-of-month month "
            f"day-of-week) or be an @alias, got {expr!r}"
        )
    minute_s, hour_s, dom_s, month_s, dow_s = fields
    dow = _field(dow_s, 0, 7, _DAYS)
    if 7 in dow:  # 0 and 7 are both Sunday
        dow = (dow - {7}) | {0}
    return CronExpr(
        minutes=frozenset(_field(minute_s, 0, 59)),
        hours=frozenset(_field(hour_s, 0, 23)),
        dom=frozenset(_field(dom_s, 1, 31)),
        months=frozenset(_field(month_s, 1, 12, _MONTHS)),
        dow=frozenset(dow),
        dom_star=dom_s.strip() == "*",
        dow_star=dow_s.strip() == "*",
    )
