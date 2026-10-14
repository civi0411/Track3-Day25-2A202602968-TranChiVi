"""Part E — Multi-window, multi-burn-rate alerting (Google SRE Workbook, ch. 5).

Input: per-minute ``total`` and ``bad`` request counts for a 30-day window.

burn_rate(window) = error_ratio(window) / (1 - SLO)

A rule fires at minute i when BOTH its long and its short window (ending at i,
inclusive) burn at least ``burn_rate``. The long window says "this is
significant"; the short window says "it is still happening" (so the alert
resets quickly after recovery).

Tests: tests/test_alerting.py, then `make slo`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str  # "page" or "ticket"
    long_min: int
    short_min: int
    burn_rate: float


# SRE Workbook standard alerting rules for a 30-day (43,200 min) SLO window
DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule("fast_burn_2pct_1h", "page", 60, 5, 14.4),
    AlertRule("slow_burn_5pct_6h", "page", 360, 30, 6.0),
    AlertRule("budget_leak_10pct_3d", "ticket", 4320, 360, 1.0),
)


@dataclass(frozen=True)
class AlertEvent:
    rule: str
    severity: str
    start_min: int  # first minute rule fired
    end_min: int  # first minute rule stopped firing (exclusive)


class WindowSums:
    """O(1) prefix sum data structure for rolling window aggregations."""

    def __init__(self, values: list[int]) -> None:
        """Construct cumulative prefix sum array."""
        self._prefix = [0]
        accum = 0
        for val in values:
            accum += val
            self._prefix.append(accum)
        self._len = len(values)

    def sum(self, end_inclusive: int, length: int) -> int:
        """Calculate sum of slice ending at end_inclusive of specified length."""
        if self._len == 0:
            return 0

        bounded_end = min(end_inclusive, self._len - 1)
        if bounded_end < 0:
            return 0

        bounded_start = max(0, end_inclusive - length + 1)
        if bounded_start > bounded_end:
            return 0

        return self._prefix[bounded_end + 1] - self._prefix[bounded_start]


class BurnRateAlerter:
    def __init__(self, slo: float = 0.995, rules: tuple[AlertRule, ...] = DEFAULT_RULES) -> None:
        if not (0.0 < slo < 1.0):
            raise ValueError("slo must be between 0 and 1 exclusive")
        self.slo = slo
        self.rules = rules

    def burn_rate(self, total: WindowSums, bad: WindowSums, end: int, length: int) -> float:
        """Calculate burn rate factor normalized by (1 - SLO)."""
        tot_count = total.sum(end, length)
        if tot_count == 0:
            return 0.0
        error_rate = bad.sum(end, length) / tot_count
        return error_rate / (1.0 - self.slo)

    def evaluate(self, total: list[int], bad: list[int]) -> list[AlertEvent]:
        """Detect firing alert episodes using dual-window burn rate conditions."""
        total_sums = WindowSums(total)
        bad_sums = WindowSums(bad)
        num_minutes = len(total)
        events: list[AlertEvent] = []

        for rule in self.rules:
            active_episode_start: int | None = None

            for minute_idx in range(num_minutes):
                long_br = self.burn_rate(total_sums, bad_sums, minute_idx, rule.long_min)
                short_br = self.burn_rate(total_sums, bad_sums, minute_idx, rule.short_min)

                is_firing = (long_br >= rule.burn_rate) and (short_br >= rule.burn_rate)

                if is_firing:
                    if active_episode_start is None:
                        active_episode_start = minute_idx
                else:
                    if active_episode_start is not None:
                        events.append(AlertEvent(rule.name, rule.severity, active_episode_start, minute_idx))
                        active_episode_start = None

            if active_episode_start is not None:
                events.append(AlertEvent(rule.name, rule.severity, active_episode_start, num_minutes))

        events.sort(key=lambda ev: (ev.start_min, ev.rule))
        return events


def error_budget_report(total: list[int], bad: list[int], slo: float = 0.995) -> dict[str, float | bool]:
    """Calculate 30-day error budget consumption and release freeze recommendation."""
    total_reqs = sum(total)
    allowed_bad = round((1.0 - slo) * total_reqs, 1)
    actual_bad = sum(bad)

    availability = round(1.0 - (actual_bad / total_reqs), 6) if total_reqs > 0 else 1.0
    budget_consumed = round(actual_bad / allowed_bad, 4) if allowed_bad > 0 else 0.0
    budget_remaining = round(1.0 - budget_consumed, 4)
    freeze_releases = budget_remaining <= 0.0

    return {
        "requests": total_reqs,
        "allowed_bad": allowed_bad,
        "bad": actual_bad,
        "availability": availability,
        "budget_consumed": budget_consumed,
        "budget_remaining": budget_remaining,
        "freeze_releases": freeze_releases,
    }
