"""Part D — Exactly-once *effects* for an agent workflow.

Two essential ingredients:
1. Idempotency keys: deterministic, derived from (session_id, step)
2. Durable Checkpoints: committed immediately AFTER step completion

Never catch WorkerCrash — it simulates process termination.

Tests: tests/test_executor.py, then `make workflows`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from gameday_lab.workflows.runner import STEPS, RefundRequest
from gameday_lab.workflows.services import DurableStore, ServiceError, Services, ServiceTimeout

T = TypeVar("T")


def idempotency_key(req: RefundRequest, step: str) -> str:
    """Deterministic idempotency key specific to session and step."""
    return f"{req.session_id}:{step}"


def checkpoint_key(req: RefundRequest, step: str) -> str:
    """Storage key for step checkpoint in DurableStore."""
    return f"checkpoint:{req.session_id}:{step}"


class WorkflowExecutor:
    def __init__(self, services: Services, store: DurableStore, max_attempts: int = 5) -> None:
        self.s = services
        self.store = store
        self.max_attempts = max_attempts

    def _retry(self, operation: Callable[[], T]) -> T:
        """Execute operation with retry on transient network errors.

        Retries ServiceError and ServiceTimeout up to max_attempts times.
        Never catches WorkerCrash.
        """
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            try:
                return operation()
            except (ServiceError, ServiceTimeout) as err:
                last_error = err

        if last_error is not None:
            raise last_error
        raise RuntimeError("No attempts executed")

    def _step(self, req: RefundRequest, state: dict[str, Any], step: str) -> Any:
        """Execute individual workflow step."""
        if step == "classify_request":
            return self._retry(lambda: self.s.llm.classify(req.order_id, req.amount_cents))

        if step == "lookup_order":
            order_id = state["classify_request"]["order_id"]
            return self._retry(lambda: self.s.orders.get(order_id))

        if step == "issue_refund":
            idemp_key = idempotency_key(req, step)
            return self._retry(lambda: self.s.payments.refund(req.order_id, req.amount_cents, idempotency_key=idemp_key))

        if step == "send_email":
            idemp_key = idempotency_key(req, step)
            email_payload = {"refund_id": state["issue_refund"]["refund_id"]}
            return self._retry(
                lambda: self.s.email.send(req.customer_email, "refund_done", email_payload, idempotency_key=idemp_key)
            )

        if step == "write_summary":
            ref_id = state["issue_refund"]["refund_id"]
            msg_id = state["send_email"]["message_id"]
            return self._retry(lambda: self.s.llm.summarize(ref_id, msg_id))

        raise ValueError(f"Unknown step: {step}")

    def run(self, req: RefundRequest) -> dict[str, Any]:
        """Execute refund workflow exactly once across worker restarts."""
        state: dict[str, Any] = {}

        for step in STEPS:
            ckey = checkpoint_key(req, step)
            saved_result = self.store.get(ckey)

            if saved_result is not None:
                # Step was already completed by previous worker: resume from checkpoint
                state[step] = saved_result
            else:
                # Step not yet done: execute, then checkpoint atomically
                step_result = self._step(req, state, step)
                self.store.put(ckey, step_result)
                state[step] = step_result

        return {"status": "done", **state}
