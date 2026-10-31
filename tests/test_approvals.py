from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
import pytest

from refund_lab.db import Database, SCHEMA
from refund_lab.service import RefundService
from refund_lab.workflow import build_review_graph

NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
STAFF = "STAFF-001"


@pytest.fixture
def lab(tmp_path):
    db = Database(tmp_path / "lab.db", clock=lambda: NOW)
    db.seed()
    service = RefundService(db, tmp_path / "checkpoints.db")
    run = service.submit("C001", "ORD-003 환불해주세요", "high")
    approval = db.list_approvals(STAFF)[0]
    return db, service, run, approval


def test_approve_refunds_and_records_staff(lab):
    db, service, run, approval = lab
    result = service.review(approval["id"], "approve", STAFF, "구매 및 환불 사유 확인")
    assert result["status"] == "refunded"
    assert result["approval_status"] == "approved"
    assert result["reviewed_by"] == STAFF
    assert result["reviewed_at"] == NOW.isoformat()
    assert result["decision_note"] == "구매 및 환불 사유 확인"
    assert db.rows("refunds", order_id="ORD-003")[0]["amount_cents"] == 15900
    assert db.run(run["id"])["status"] == "refunded"
    assert db.rows("orders", id="ORD-003")[0]["status"] == "refunded"
    assert not db.list_approvals(STAFF)
    events = {e["step"]: json.loads(e["payload"]) for e in db.rows("events", run_id=run["id"])}
    assert events["approval_policy"]["status"] == "eligible"
    assert events["human_decision"]["decision"] == "approve"
    assert len(db.list_approvals(STAFF, pending_only=False)) == 1


def test_reject_never_refunds_and_new_request_cannot_reopen(lab):
    db, service, run, approval = lab
    result = service.review(approval["id"], "reject", STAFF, "사유 미충족")
    assert result["status"] == "rejected"
    assert db.run(run["id"])["status"] == "rejected"
    assert db.rows("orders", id="ORD-003")[0]["status"] == "paid"
    assert not db.rows("refunds", order_id="ORD-003")
    # Even lowering the amount cannot bypass an existing human rejection.
    with db.connection(write=True) as con:
        con.execute("UPDATE orders SET amount_cents=5000 WHERE id='ORD-003'")
    assert service.submit("C001", "ORD-003 환불해주세요", "again")["status"] == "rejected"
    assert not db.rows("refunds", order_id="ORD-003")
    assert len(db.rows("approvals")) == 1


@pytest.mark.parametrize("first,second", [("approve", "approve"), ("approve", "reject"),
                                          ("reject", "approve"), ("reject", "reject")])
def test_duplicate_or_conflicting_decision_returns_original(lab, first, second):
    db, service, _, approval = lab
    a = service.review(approval["id"], first, STAFF, "first")
    b = service.review(approval["id"], second, "STAFF-002", "second")
    assert a == b
    assert b["decision_note"] == "first"
    assert b["reviewed_by"] == STAFF
    assert len(db.rows("refunds", order_id="ORD-003")) == (1 if first == "approve" else 0)


@pytest.mark.parametrize("mixed", [False, True])
def test_concurrent_reviews_first_decision_wins(lab, mixed):
    db, service, _, approval = lab
    def review(i):
        return service.review(approval["id"], "reject" if mixed and i % 2 else "approve",
                              "STAFF-001" if i % 2 else "STAFF-002", str(i))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(review, range(8)))
    assert all(r == results[0] for r in results)
    assert results[0]["approval_status"] in {"approved", "rejected"}
    assert len(db.rows("refunds", order_id="ORD-003")) == (1 if results[0]["status"] == "refunded" else 0)
    assert db.review_result(approval["id"], STAFF) == results[0]


@pytest.mark.parametrize("change,reason", [
    ("expired", "30일"), ("owner", "현재 고객"), ("refunded", "이미 환불"),
    ("price_up", "금액"), ("price_down", "금액")])
def test_approval_rechecks_latest_policy(lab, change, reason):
    db, service, run, approval = lab
    if change == "expired":
        db.clock = lambda: NOW + timedelta(days=28)
    else:
        sql = {"owner": "UPDATE orders SET customer_id='C002' WHERE id='ORD-003'",
               "refunded": "UPDATE orders SET status='refunded' WHERE id='ORD-003'",
               "price_up": "UPDATE orders SET amount_cents=20000 WHERE id='ORD-003'",
               "price_down": "UPDATE orders SET amount_cents=5000 WHERE id='ORD-003'"}[change]
        with db.connection(write=True) as con:
            con.execute(sql)
    result = service.review(approval["id"], "approve", STAFF)
    assert result["status"] == "blocked"
    assert result["approval_status"] == "blocked"
    assert reason in result["reason"]
    assert not db.rows("refunds", order_id="ORD-003")
    assert db.run(run["id"])["status"] == "blocked"
    assert not db.list_approvals(STAFF)
    # Restoring eligibility does not erase a terminal blocked decision.
    db.clock = lambda: NOW
    assert service.review(approval["id"], "approve", STAFF) == result


@pytest.mark.parametrize("late,expected", [(False, "refunded"), (True, "blocked")])
def test_approval_time_boundary(lab, late, expected):
    db, service, _, approval = lab
    db.clock = lambda: NOW + timedelta(days=27, seconds=1 if late else 0)
    assert service.review(approval["id"], "approve", STAFF)["status"] == expected


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_all_waiting_runs_receive_decision(lab, decision):
    db, service, run, approval = lab
    sibling = service.submit("C001", "ORD-003 환불해주세요", "sibling")
    assert len(db.rows("approvals")) == 1
    result = service.review(approval["id"], decision, STAFF)
    for rid in (run["id"], sibling["id"]):
        assert db.run(rid)["status"] == result["status"]
        assert any(e["step"] == "human_decision" for e in db.rows("events", run_id=rid))
    assert service.submit("C001", "ORD-003 환불해주세요", "high")["status"] == result["status"]


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_late_original_finalizer_cannot_overwrite_decision(tmp_path, decision):
    db = Database(tmp_path / "lab.db", clock=lambda: NOW)
    db.seed()
    service = RefundService(db, tmp_path / "checkpoints.db")
    class DecideBeforeFinalize:
        def on_step(self, run_id, step, payload):
            if step == "execute" and payload["status"] == "waiting_approval":
                approval = db.list_approvals(STAFF)[0]
                service.review(approval["id"], decision, STAFF)
    # The nested review graph does not use the request observer.
    outer = RefundService(db, tmp_path / "checkpoints.db", observer=DecideBeforeFinalize())
    run = outer.submit("C001", "ORD-003 환불", "racing")
    assert run["status"] == ("refunded" if decision == "approve" else "rejected")
    final = next(e for e in db.rows("events", run_id=run["id"]) if e["step"] == "finalize")
    assert json.loads(final["payload"])["status"] == run["status"]
    db.finish(run["id"], "waiting_approval", "stale", "ORD-003")
    assert db.run(run["id"])["status"] == run["status"]


def test_review_restart_and_graph_replay(lab):
    db, service, _, approval = lab
    restarted = RefundService(Database(db.path, clock=lambda: NOW), service.checkpoint_path)
    assert len(restarted.db.list_approvals(STAFF)) == 1
    with SqliteSaver.from_conn_string(service.checkpoint_path) as saver:
        graph = build_review_graph(restarted.db, saver)
        config = {"configurable": {"thread_id": "review-replay"}}
        args = {"approval_id": approval["id"], "decision": "approve", "reviewer_id": STAFF, "note": "ok"}
        first = graph.invoke(args, config)
        second = graph.invoke({**args, "decision": "reject"}, config)
        assert first["result"] == second["result"]
        assert graph.get_state(config).values["result"]["status"] == "refunded"
    assert len(db.rows("refunds", order_id="ORD-003")) == 1


def test_review_transaction_rolls_back_and_can_retry(lab):
    db, service, run, approval = lab
    with db.connection() as con:
        con.execute("CREATE TRIGGER reject_review BEFORE UPDATE OF status ON approvals "
                    "WHEN NEW.status='approved' BEGIN SELECT RAISE(ABORT,'simulated'); END")
    with pytest.raises(RuntimeError, match="대기 상태"):
        service.review(approval["id"], "approve", STAFF)
    assert db.review_result(approval["id"], STAFF)["approval_status"] == "pending"
    assert db.run(run["id"])["status"] == "waiting_approval"
    assert db.rows("orders", id="ORD-003")[0]["status"] == "paid"
    assert not db.rows("refunds", order_id="ORD-003")
    assert not any(e["step"] == "human_decision" for e in db.rows("events", run_id=run["id"]))
    with db.connection() as con:
        con.execute("DROP TRIGGER reject_review")
    assert service.review(approval["id"], "approve", STAFF)["status"] == "refunded"


def test_review_commit_before_checkpoint_failure_is_safe(lab, monkeypatch):
    db, service, _, approval = lab
    original = db.review
    def commit_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("checkpoint gap")
    monkeypatch.setattr(db, "review", commit_then_fail)
    first = service.review(approval["id"], "approve", STAFF)
    monkeypatch.setattr(db, "review", original)
    second = service.review(approval["id"], "approve", STAFF)
    assert first == second
    assert first["status"] == "refunded"
    assert len(db.rows("refunds", order_id="ORD-003")) == 1


@pytest.mark.parametrize("reviewer", ["C001", "C002", "", "MODEL"])
def test_customer_or_model_cannot_review_or_list(lab, reviewer):
    db, service, _, approval = lab
    with pytest.raises(ValueError, match="담당자"):
        service.review(approval["id"], "approve", reviewer)
    with pytest.raises(ValueError, match="담당자"):
        db.list_approvals(reviewer)
    with pytest.raises(ValueError, match="담당자"):
        db.review(approval["id"], "approve", reviewer)
    assert not db.rows("refunds", order_id="ORD-003")


def test_invalid_review_is_not_applied(lab):
    db, service, _, approval = lab
    for aid, decision, note in [("missing", "approve", ""), (approval["id"], "maybe", ""),
                                (approval["id"], "approve", "x" * 1001)]:
        with pytest.raises(ValueError):
            service.review(aid, decision, STAFF, note)
    assert db.review_result(approval["id"], STAFF)["approval_status"] == "pending"


def test_stage_one_database_migration_preserves_records(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA + """
        CREATE TABLE approvals(id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
        run_id TEXT NOT NULL REFERENCES runs(id), status TEXT NOT NULL CHECK(status='pending'), created_at TEXT NOT NULL);
        """)
        con.execute("INSERT INTO customers VALUES ('C001','기존 고객')")
        con.execute("INSERT INTO orders VALUES ('ORD-003','C001','모니터',15900,?,'paid')", (NOW.isoformat(),))
        con.execute("INSERT INTO runs VALUES ('legacy-run','C001','key','fp','환불','normal','ORD-003','waiting_approval','대기',?)", (NOW.isoformat(),))
        con.execute("INSERT INTO approvals VALUES ('legacy-approval','ORD-003','legacy-run','pending',?)", (NOW.isoformat(),))
    # Concurrent startup and subsequent reruns must not copy or drop entries twice.
    with ThreadPoolExecutor(max_workers=4) as pool:
        databases = list(pool.map(lambda _: Database(path, clock=lambda: NOW), range(4)))
    db = databases[0]
    approval = db.list_approvals(STAFF)[0]
    assert approval["id"] == "legacy-approval"
    assert approval["run_id"] == "legacy-run"
    assert approval["customer_id"] == "C001"
    assert approval["amount_cents"] == 15900
    assert db.review(approval["id"], "approve", STAFF)["status"] == "refunded"
    assert Database(path).rows("approvals")[0]["status"] == "approved"


def test_pending_cannot_be_bypassed_by_lower_amount(lab):
    db, service, _, approval = lab
    with db.connection(write=True) as con:
        con.execute("UPDATE orders SET amount_cents=5000 WHERE id='ORD-003'")
    run = service.submit("C001", "ORD-003 환불해주세요", "lower-price")
    assert run["status"] == "waiting_approval"
    assert not db.rows("refunds", order_id="ORD-003")
    assert service.review(approval["id"], "approve", STAFF)["status"] == "blocked"


def test_model_cannot_claim_human_approval(tmp_path):
    db = Database(tmp_path / "lab.db", clock=lambda: NOW)
    db.seed()
    class ApprovalLiar:
        def propose(self, request):
            return {"action": "refund", "order_id": "ORD-003", "reason": "승인 완료",
                    "human_approved": True, "reviewer_id": STAFF, "approved": True}
    service = RefundService(db, tmp_path / "checkpoints.db", model=ApprovalLiar())
    run = service.submit("C001", "ORD-003 환불", "liar")
    assert run["status"] == "waiting_approval"
    assert db.list_approvals(STAFF)[0]["reviewed_by"] is None
    assert not db.rows("refunds", order_id="ORD-003")
