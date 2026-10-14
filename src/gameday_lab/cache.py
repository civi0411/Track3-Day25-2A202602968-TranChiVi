"""Part G — a semantic response cache that is safe, fresh, and keeps FAQ answers up in an outage.

* ``SemanticCache``: lookup by embedding similarity *within a scope* (prompt
  version; personal questions never), TTL + a stale window, invalidation by
  source document, bounded size (LRU).
* ``CachingClient``: wraps your ResilientClient. A fresh hit answers without
  calling any provider; a miss goes to the providers and the answer is stored;
  when the providers give up, a *stale* answer beats no answer (fallback
  ladder, rung 3: "cached response").

Tests: tests/test_cache.py, then `make cache` (tape C1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from gameday_lab.sim.contracts import (
    Action,
    Answer,
    Attempt,
    BaseClient,
    CallContext,
    GiveUp,
    ProviderError,
    Request,
    Serve,
)


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity: 1.0 = identical direction (high = strict threshold)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Entry:
    embedding: tuple[float, ...]
    answer: Answer
    stored_at: float
    last_used: float


@dataclass(frozen=True)
class CacheHit:
    answer: Answer
    similarity: float
    age_s: float
    fresh: bool  # age_s <= ttl_s; stale hit served only when provider cannot answer


class SemanticCache:
    def __init__(
        self,
        similarity_threshold: float = 0.90,
        ttl_s: float = 60.0,
        max_stale_s: float = 300.0,
        max_entries: int = 500,
    ) -> None:
        """Initialize semantic cache configuration."""
        self.similarity_threshold = similarity_threshold
        self.ttl_s = ttl_s
        self.max_stale_s = max_stale_s
        self.max_entries = max_entries
        self.entries: list[Entry] = []

    def _purge_expired(self, now: float) -> None:
        """Prune entries exceeding maximum allowed lifespan (TTL + stale window)."""
        max_lifetime = self.ttl_s + self.max_stale_s
        self.entries = [entry for entry in self.entries if (now - entry.stored_at) <= max_lifetime]

    def lookup(self, req: Request, now: float) -> CacheHit | None:
        """Find the most similar valid entry within prompt version scope."""
        if req.personal:
            return None

        self._purge_expired(now)

        best_match: Entry | None = None
        best_score = -1.0

        for item in self.entries:
            if item.answer.prompt_version != req.prompt_version:
                continue

            sim_score = cosine(req.embedding, item.embedding)
            if sim_score > best_score:
                best_score = sim_score
                best_match = item

        if best_match is not None and best_score >= self.similarity_threshold:
            best_match.last_used = now
            age = now - best_match.stored_at
            return CacheHit(
                answer=best_match.answer,
                similarity=best_score,
                age_s=age,
                fresh=(age <= self.ttl_s),
            )

        return None

    def store(self, req: Request, answer: Answer, now: float) -> None:
        """Store or update response for non-personal requests."""
        if req.personal:
            return

        best_index: int | None = None
        highest_sim = -1.0

        for idx, item in enumerate(self.entries):
            if item.answer.prompt_version != req.prompt_version:
                continue
            sim = cosine(req.embedding, item.embedding)
            if sim > highest_sim:
                highest_sim = sim
                best_index = idx

        if best_index is not None and highest_sim >= self.similarity_threshold:
            # Refresh existing entry with newest answer and timestamp
            self.entries[best_index] = Entry(req.embedding, answer, now, now)
        else:
            # Append new entry; evict LRU if capacity exceeded
            self.entries.append(Entry(req.embedding, answer, now, now))
            if len(self.entries) > self.max_entries:
                oldest_idx = min(range(len(self.entries)), key=lambda i: self.entries[i].last_used)
                self.entries.pop(oldest_idx)

    def invalidate(self, source: str, now: float) -> int:
        """Evict all cache entries derived from specified source document."""
        initial_count = len(self.entries)
        self.entries = [e for e in self.entries if source not in e.answer.sources]
        return initial_count - len(self.entries)


class CachingClient(BaseClient):
    name = "cached"

    def __init__(self, inner: BaseClient, cache: SemanticCache) -> None:
        """Wrap inner client with semantic caching and stale-if-error logic."""
        self.inner = inner
        self.cache = cache

    def on_cancel(self, call: CallContext, tag: Any, now: float) -> None:
        """Delegate cancellation to inner client."""
        self.inner.on_cancel(call, tag, now)

    def _or_stale(self, call: CallContext, action: Action, now: float) -> Action:
        """Stale-if-error fallback: serve stale cached answer when provider gives up."""
        if isinstance(action, GiveUp) and call.request is not None:
            hit = self.cache.lookup(call.request, now)
            if hit is not None:
                return Serve(hit.answer, stale=not hit.fresh)
        return action

    def start(self, call: CallContext, now: float) -> Action:
        """Serve fresh FAQ hits immediately; otherwise delegate to inner client."""
        if call.request is not None:
            hit = self.cache.lookup(call.request, now)
            if hit is not None and hit.fresh:
                return Serve(hit.answer, stale=False)

        inner_action = self.inner.start(call, now)
        return self._or_stale(call, inner_action, now)

    def on_success(self, attempt: Attempt, now: float, latency_s: float) -> None:
        """Record success in inner client and populate cache with response."""
        self.inner.on_success(attempt, now, latency_s)
        call = attempt.call
        if call.request is not None and call.answer is not None:
            self.cache.store(call.request, call.answer, now)

    def on_error(self, attempt: Attempt, err: ProviderError, now: float) -> Action:
        """Handle error from inner client with stale-if-error fallback."""
        action = self.inner.on_error(attempt, err, now)
        return self._or_stale(attempt.call, action, now)

    def on_content_update(self, source: str, now: float) -> None:
        """Invalidate cache entries upon document update webhook."""
        self.cache.invalidate(source, now)
