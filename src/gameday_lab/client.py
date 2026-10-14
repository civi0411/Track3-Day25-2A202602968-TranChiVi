"""Part C — A resilient LLM client that survives incidents without breaking providers.

Combines Part A (classification, jittered backoff, retry budget) and Part B
(circuit breaker) into an event-driven client that runs on a simulated clock.

Decision Flow:
--------------
* Task capability: filter fallback ladder (e.g. tool-calling requires provider support)
* Routing: walk ladder, check cooldown and circuit breaker state
* Error handling:
    - CLIENT_ERROR (400, 401, 422): release permit, give up immediately (no retry, no failover)
    - QUOTA_EXHAUSTED (429): release permit, enter long cooldown (300s), failover immediately
    - RATE_LIMITED (429): release permit, set cooldown from retry_after, failover immediately
    - OVERLOADED / SERVER_ERROR / TIMEOUT:
        * record failure to breaker
        * retry on SAME provider if tries < max_attempts, breaker CLOSED/HALF-OPEN, and budget available
        * otherwise failover to next ladder rung

Tests: tests/test_client.py
"""

from __future__ import annotations

import random
from typing import Any

from gameday_lab.breaker import BreakerState, Permit, SlidingWindowBreaker
from gameday_lab.errors import ErrorKind, classify
from gameday_lab.retry import FullJitterBackoff, RetryBudget
from gameday_lab.sim.contracts import (
    Action,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    ProviderInfo,
    Send,
)

# Maximum cooldown threshold (seconds) for waiting on a rate-limited provider
MAX_RATE_LIMIT_WAIT_S = 5.0


class ResilientClient(BaseClient):
    name = "resilient"

    def __init__(
        self,
        providers: dict[str, ProviderInfo],
        fallback_order: list[str] | tuple[str, ...],
        settings: dict[str, Any],
        seed: int = 0,
    ) -> None:
        """Initialize client settings, breakers, backoff and retry budget."""
        self.providers = providers
        self.order = list(fallback_order)
        self.attempt_timeout_s = float(settings.get("attempt_timeout_s", 7.5))
        self.max_attempts_per_provider = int(settings.get("max_attempts_per_provider", 2))
        self.min_time_left_s = float(settings.get("min_time_left_s", 1.0))
        self.quota_cooldown_s = float(settings.get("quota_cooldown_s", 300.0))

        # Backoff engine with full jitter
        base_s = float(settings.get("backoff_base_s", 0.2))
        cap_s = float(settings.get("backoff_cap_s", 3.5))
        self.backoff = FullJitterBackoff(base_s, cap_s, random.Random(seed))

        # Retry budget token bucket
        budget_ratio = float(settings.get("retry_budget_ratio", 0.1))
        budget_min = float(settings.get("retry_budget_min_per_s", 0.5))
        budget_max = float(settings.get("retry_budget_max_tokens", 20.0))
        self.budget = RetryBudget(ratio=budget_ratio, min_per_s=budget_min, max_tokens=budget_max)

        # Per-provider circuit breakers and cooldown trackers
        breaker_conf = dict(settings.get("breaker", {}))
        self.breakers = {name: SlidingWindowBreaker(name, **breaker_conf) for name in providers}
        self.cooldown_until: dict[str, float] = {name: 0.0 for name in providers}

    def _send(self, call: CallContext, provider: str, now: float, delay: float) -> Action | None:
        """Build a Send action for provider after delay, or None if breaker refuses.

        Enforces call deadline constraints and releases permit if insufficient time remains.
        """
        permit = self.breakers[provider].allow(now)
        if permit is None:
            return None

        scheduled_send_time = now + delay
        remaining_time = call.deadline - scheduled_send_time
        effective_timeout = min(self.attempt_timeout_s, remaining_time)

        if effective_timeout < self.min_time_left_s:
            self.breakers[provider].release(permit, now)
            return GiveUp("deadline")

        return Send(provider, timeout_s=effective_timeout, delay_s=delay, tag=permit)

    def ladder(self, task: str) -> list[str]:
        """Filter configured fallback order by capability for task."""
        if task == "tool":
            return [name for name in self.order if self.providers[name].supports_tools]
        return list(self.order)

    def _route(self, call: CallContext, now: float, delay: float = 0.0) -> Action:
        """Route to next available provider in ladder or wait for short cooldown."""
        ladder = call.state["ladder"]
        idx = call.state["idx"]

        # Step 1: Scan current and subsequent ladder rungs
        while idx < len(ladder):
            provider_candidate = ladder[idx]
            target_time = now + delay

            if self.cooldown_until[provider_candidate] > target_time:
                # Provider is in cooldown: skip to next rung immediately
                idx += 1
                call.state["idx"] = idx
                call.state["tries"] = 0
                delay = 0.0
                continue

            action = self._send(call, provider_candidate, now, delay)
            if action is not None:
                return action

            # Breaker blocked the request: skip to next rung immediately
            idx += 1
            call.state["idx"] = idx
            call.state["tries"] = 0
            delay = 0.0

        # Step 2: Ladder exhausted -> look for shortest viable rate limit cooldown
        viable_waits: list[tuple[float, int, str]] = []
        for rung_idx, prov in enumerate(ladder):
            if self.breakers[prov].state is BreakerState.OPEN:
                continue

            wait_duration = self.cooldown_until[prov] - now
            if 0.0 < wait_duration <= MAX_RATE_LIMIT_WAIT_S:
                send_at = now + wait_duration
                if (call.deadline - send_at) >= self.min_time_left_s:
                    viable_waits.append((wait_duration, rung_idx, prov))

        if viable_waits:
            viable_waits.sort(key=lambda item: (item[0], item[1]))
            best_wait, best_idx, best_provider = viable_waits[0]
            call.state["idx"] = best_idx
            call.state["tries"] = 0
            retry_action = self._send(call, best_provider, now, delay=best_wait)
            if retry_action is not None:
                return retry_action

        return GiveUp("no_provider_available")

    def start(self, call: CallContext, now: float) -> Action:
        """Initiate call context, record into budget, and route."""
        self.budget.record_request(now)
        call.state["ladder"] = self.ladder(call.task)
        call.state["idx"] = 0
        call.state["tries"] = 0
        return self._route(call, now, delay=0.0)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """Report successful latency to breaker."""
        self.breakers[attempt.provider].on_success(attempt.tag, now, latency_s)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """Process error according to error classification rules."""
        err_kind = classify(err)
        provider = attempt.provider
        call = attempt.call

        if err_kind is ErrorKind.CLIENT_ERROR:
            self.breakers[provider].release(attempt.tag, now)
            return GiveUp("client_error")

        if err_kind is ErrorKind.QUOTA_EXHAUSTED:
            self.breakers[provider].release(attempt.tag, now)
            self.cooldown_until[provider] = max(self.cooldown_until[provider], now + self.quota_cooldown_s)
            call.state["idx"] += 1
            call.state["tries"] = 0
            return self._route(call, now, delay=0.0)

        if err_kind is ErrorKind.RATE_LIMITED:
            self.breakers[provider].release(attempt.tag, now)
            cooldown_period = err.retry_after if err.retry_after is not None else 1.0
            self.cooldown_until[provider] = max(self.cooldown_until[provider], now + cooldown_period)
            call.state["idx"] += 1
            call.state["tries"] = 0
            return self._route(call, now, delay=0.0)

        # Transient server errors: OVERLOADED, SERVER_ERROR, TIMEOUT
        self.breakers[provider].on_failure(attempt.tag, now)
        call.state["tries"] += 1

        # Check if we should retry on the same provider
        should_retry = (
            call.state["tries"] < self.max_attempts_per_provider
            and self.breakers[provider].state is not BreakerState.OPEN
            and self.budget.try_acquire_retry(now)
        )

        if should_retry:
            backoff_delay = self.backoff.delay(call.state["tries"], retry_after=err.retry_after)
            action = self._send(call, provider, now, backoff_delay)
            if action is not None and isinstance(action, Send):
                return action

        # Failover to next provider
        call.state["idx"] += 1
        call.state["tries"] = 0
        return self._route(call, now, delay=0.0)

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """Release permit when pending delayed attempt is cancelled."""
        if isinstance(tag, Permit):
            self.breakers[tag.breaker].release(tag, now)
