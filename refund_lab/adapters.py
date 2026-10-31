"""Replaceable boundaries for models, future MCP tools, and Langfuse tracing."""
import re
from typing import Any, Protocol, TypedDict


class Proposal(TypedDict):
    action: str
    order_id: str | None
    reason: str


class ModelAdapter(Protocol):
    def propose(self, request: str) -> Proposal: ...


class FakeModel:
    """Offline and intentionally simple; never receives session identity."""
    def __init__(self, reckless: bool = False):
        self.reckless = reckless

    def propose(self, request: str) -> Proposal:
        ids = list(dict.fromkeys(re.findall(r"\bORD-\d{3}\b", request.upper())))
        if len(ids) != 1:
            return {"action": "clarify", "order_id": None,
                    "reason": "ORD-001 형식의 주문 번호를 하나만 알려주세요."}
        negative = any(word in request.lower() for word in
                       ("취소하지", "환불하지", "don't refund", "do not refund"))
        refund = "환불" in request or "refund" in request.lower()
        inquiry = any(word in request.lower() for word in (
            "환불 가능", "환불 정책", "환불할 수", "가능한지", "상태", "절차", "방법",
            "문의", "can i", "eligible", "refund policy", "refund status"))
        action = "refund" if self.reckless or (refund and not negative and not inquiry) else "clarify"
        return {"action": action, "order_id": ids[0],
                "reason": "가짜 모델의 전액 환불 제안" if action == "refund"
                else "환불을 원하시는지 확인해주세요."}


class RefundTools(Protocol):
    """A future MCP adapter must still call the policy-enforcing server boundary."""
    def lookup(self, customer_id: str, order_id: str) -> dict[str, Any] | None: ...
    def execute(self, run_id: str) -> dict[str, Any]: ...


class Observer(Protocol):
    """A Langfuse observer can be added without changing business decisions."""
    def on_step(self, run_id: str, step: str, payload: dict[str, Any]) -> None: ...


class NullObserver:
    def on_step(self, run_id: str, step: str, payload: dict[str, Any]) -> None:
        pass
