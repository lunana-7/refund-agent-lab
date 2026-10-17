"""The authority for refunds. Model output never overrides these rules."""
from datetime import datetime, timedelta, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def evaluate(order: dict[str, Any] | None, customer_id: str,
             now: datetime, *, human_approved: bool = False) -> tuple[str, str]:
    if order is None or order["customer_id"] != customer_id:
        # Deliberately identical: do not disclose somebody else's order.
        return "blocked", "주문을 찾을 수 없거나 현재 고객의 주문이 아닙니다."
    if order["status"] == "refunded":
        return "blocked", "이미 환불된 주문입니다."
    purchased = datetime.fromisoformat(order["purchased_at"])
    if purchased.tzinfo is None:
        return "blocked", "구매 시각을 확인할 수 없습니다."
    age = now - purchased
    if age < timedelta(0) or age > timedelta(days=30):
        return "blocked", "구매 후 30일 이내의 주문만 환불할 수 있습니다."
    if order["amount_cents"] > 10_000 and not human_approved:
        return "waiting_approval", "100달러를 초과하여 사람 승인이 필요합니다. 실행하지 않았습니다."
    return "eligible", ("현재 고객 소유 · 구매 후 30일 이내 · 사람 승인 확인 · 미환불"
                        if human_approved else "현재 고객 소유 · 구매 후 30일 이내 · 100달러 이하 · 미환불")
