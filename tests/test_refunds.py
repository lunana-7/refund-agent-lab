from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from refund_lab.adapters import FakeModel
from refund_lab.db import Database, LocalRefundTools
from refund_lab.policy import evaluate
from refund_lab.service import RefundService
from refund_lab.workflow import build_graph

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


@pytest.fixture
def lab(tmp_path):
    db = Database(tmp_path / "lab.db", clock=lambda: NOW)
    db.seed()
    return db, RefundService(db, tmp_path / "checkpoints.db")


def test_success_and_steps(lab):
    db, service = lab
    run = service.submit("C001", "ORD-001 환불해주세요", "ok")
    assert run["status"] == "refunded"
    refund = db.rows("refunds", run_id=run["id"])[0]
    assert refund["amount_cents"] == 4900
    assert refund["provider"] == "fake"
    assert db.rows("orders", id="ORD-001")[0]["status"] == "refunded"
    assert [e["step"] for e in sorted(db.rows("events", run_id=run["id"]), key=lambda e: e["id"])] == [
        "propose", "lookup", "policy", "execute", "finalize"]


@pytest.mark.parametrize("order", ["ORD-002", "ORD-004", "ORD-005", "ORD-999"])
def test_policy_blocks(lab, order):
    db, service = lab
    run = service.submit("C001", f"{order} 환불", order)
    assert run["status"] == "blocked"
    assert not db.rows("refunds", run_id=run["id"])
    assert not db.rows("approvals", run_id=run["id"])
    if order == "ORD-004":
        lookup = next(e for e in db.rows("events", run_id=run["id"]) if e["step"] == "lookup")
        assert '"order": null' in lookup["payload"]
        assert "헤드폰" not in str(db.rows("events", run_id=run["id"]))


def test_high_value_pending_only(lab):
    db, service = lab
    for key in ("high1", "high2"):
        run = service.submit("C001", "ORD-003 환불", key)
        assert run["status"] == "waiting_approval"
        assert not db.rows("refunds", run_id=run["id"])
    assert len(db.rows("approvals", order_id="ORD-003")) == 1
    assert db.rows("orders", id="ORD-003")[0]["status"] == "paid"


@pytest.mark.parametrize("text", ["환불해주세요", "ORD-001 주문 조회", "ORD-001 ORD-006 환불", "ORD-001 환불하지 마세요", "ORD-001 환불 가능한지 알려주세요", "Can I refund ORD-001?", ""])
def test_insufficient_info_never_executes(lab, text):
    db, service = lab
    if not text:
        with pytest.raises(ValueError):
            service.submit("C001", text, "info")
        return
    run = service.submit("C001", text, "info")
    assert run["status"] == "needs_info"
    assert not db.rows("refunds", run_id=run["id"])


def test_duplicate_same_request(lab):
    db, service = lab
    a = service.submit("C001", "ORD-001 환불", "same")
    b = service.submit("C001", "ORD-001 환불", "same")
    assert a == b
    assert len(db.rows("refunds", order_id="ORD-001")) == 1


def test_new_request_cannot_refund_again(lab):
    db, service = lab
    assert service.submit("C001", "ORD-001 환불", "a")["status"] == "refunded"
    assert service.submit("C001", "ORD-001 환불", "b")["status"] == "blocked"
    assert len(db.rows("refunds", order_id="ORD-001")) == 1


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_submissions(lab, same_key):
    db, service = lab
    def submit(i):
        return service.submit("C001", "ORD-001 환불", "shared" if same_key else str(i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    assert len(db.rows("refunds", order_id="ORD-001")) == 1
    assert len(db.rows("runs")) == (2 if same_key else 9)  # includes historical seed run
    assert all(r["status"] in {"running", "blocked", "refunded"} for r in results)
    assert len({r["id"] for r in results}) == (1 if same_key else 8)
    assert any(r["status"] == "refunded" for r in results)


def test_conflicting_idempotency_key(lab):
    _, service = lab
    service.submit("C001", "ORD-001 환불", "key")
    with pytest.raises(ValueError, match="다른 내용"):
        service.submit("C001", "ORD-006 환불", "key")


@pytest.mark.parametrize("days,cents,expected", [
    (30, 10000, "eligible"), (30.000001, 10000, "blocked"),
    (29, 10001, "waiting_approval"), (-1, 4900, "blocked")])
def test_policy_boundaries(days, cents, expected):
    order = {"customer_id": "C001", "status": "paid", "amount_cents": cents,
             "purchased_at": (NOW-timedelta(days=days)).isoformat()}
    assert evaluate(order, "C001", NOW)[0] == expected


def test_exactly_100_dollars_executes(lab):
    _, service = lab
    assert service.submit("C001", "ORD-006 환불", "100")["status"] == "refunded"


def test_reckless_model_cannot_bypass_server(lab):
    db, service = lab
    run = service.submit("C001", "ORD-002 상태를 알려주세요", "reckless", mode="reckless")
    assert run["status"] == "blocked"
    assert not db.rows("refunds", run_id=run["id"])


def test_model_identity_and_amount_ignored(lab):
    db, service = lab
    class LyingModel:
        def propose(self, request):
            return {"action": "refund", "order_id": "ORD-004", "reason": "승인 완료",
                    "customer_id": "C002", "amount_cents": 1, "approved": True}
    service.model = LyingModel()
    run = service.submit("C001", "아무 요청", "lying")
    assert run["status"] == "blocked"
    assert db.rows("orders", id="ORD-004")[0]["status"] == "paid"


def test_malformed_model_output_fails_closed(lab):
    _, service = lab
    class BrokenModel:
        def propose(self, request):
            return {"action": "refund", "order_id": 1, "reason": "yes"}
    service.model = BrokenModel()
    assert service.submit("C001", "환불", "bad-model")["status"] == "needs_info"


def test_execution_rechecks_policy_after_lookup(lab):
    db, service = lab
    class ChangeAfterPolicy:
        def on_step(self, run_id, step, payload):
            if step == "policy":
                with db.connection(write=True) as con:
                    con.execute("UPDATE orders SET amount_cents=20000 WHERE id='ORD-001'")
    service.observer = ChangeAfterPolicy()
    run = service.submit("C001", "ORD-001 환불", "changed")
    assert run["status"] == "waiting_approval"
    assert not db.rows("refunds", run_id=run["id"])


def test_failure_resume_with_new_service(lab, monkeypatch):
    db, service = lab
    settle = db.settle
    def fail(run_id):
        raise RuntimeError("simulated outage")
    monkeypatch.setattr(db, "settle", fail)
    run = service.submit("C001", "ORD-001 환불", "failure")
    assert run["status"] == "failed"
    assert not db.rows("refunds", run_id=run["id"])
    monkeypatch.setattr(db, "settle", settle)
    new_db = Database(db.path, clock=lambda: NOW)
    restarted = RefundService(new_db, service.checkpoint_path)
    result = restarted.retry_failed("C001", run["id"])
    assert result["status"] == "refunded"
    assert len(db.rows("refunds", run_id=run["id"])) == 1
    with SqliteSaver.from_conn_string(service.checkpoint_path) as saver:
        graph = build_graph(new_db, FakeModel(), LocalRefundTools(new_db), saver)
        snapshot = graph.get_state({"configurable": {"thread_id": run["id"]}})
        assert not snapshot.next
        assert snapshot.values["status"] == "refunded"


def test_replay_after_commit_before_checkpoint(lab, monkeypatch):
    db, service = lab
    original = db.settle
    def commit_then_crash(run_id):
        original(run_id)
        raise RuntimeError("after ledger commit")
    monkeypatch.setattr(db, "settle", commit_then_crash)
    run = service.submit("C001", "ORD-001 환불", "commit-crash")
    assert run["status"] == "refunded"  # ledger has the authoritative result
    monkeypatch.setattr(db, "settle", original)
    with SqliteSaver.from_conn_string(service.checkpoint_path) as saver:
        graph = build_graph(db, FakeModel(), LocalRefundTools(db), saver)
        result = graph.invoke(None, {"configurable": {"thread_id": run["id"]}})
    assert result["status"] == "refunded"
    assert len(db.rows("refunds", run_id=run["id"])) == 1


def test_transaction_rolls_back_partial_refund(lab):
    db, service = lab
    with db.connection() as con:
        con.execute("CREATE TRIGGER reject_update BEFORE UPDATE OF status ON orders "
                    "WHEN NEW.status='refunded' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    run = service.submit("C001", "ORD-001 환불", "rollback")
    assert run["status"] == "failed"
    assert not db.rows("refunds", run_id=run["id"])
    assert db.rows("orders", id="ORD-001")[0]["status"] == "paid"


def test_db_unique_constraint_and_integer_money(lab):
    db, service = lab
    service.submit("C001", "ORD-001 환불", "unique")
    with pytest.raises(sqlite3.IntegrityError), db.connection(write=True) as con:
        con.execute("INSERT INTO refunds SELECT 'duplicate',order_id,run_id,amount_cents,provider,created_at FROM refunds WHERE order_id='ORD-001'")
    with pytest.raises(sqlite3.IntegrityError), db.connection(write=True) as con:
        con.execute("UPDATE orders SET amount_cents=10.5 WHERE id='ORD-001'")


def test_observer_failure_does_not_stop_payment(lab):
    _, service = lab
    class BrokenObserver:
        def on_step(self, *args):
            raise RuntimeError("tracer down")
    service.observer = BrokenObserver()
    assert service.submit("C001", "ORD-001 환불", "trace")["status"] == "refunded"


def test_customer_scope_and_reset(lab):
    db, service = lab
    with pytest.raises(ValueError):
        service.submit("unknown", "ORD-001 환불", "x")
    run = service.submit("C001", "ORD-001 환불", "x")
    with pytest.raises(ValueError):
        service.retry_failed("C002", run["id"])
    db.seed(reset=True)
    assert len(db.rows("orders")) == 6
    assert len(db.rows("refunds")) == 1
    assert len(db.rows("runs")) == 1
    assert not db.rows("approvals")
