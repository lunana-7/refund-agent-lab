"""SQLite business ledger. Every connection has its own transaction boundary."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Callable
import uuid

from .policy import evaluate, utcnow
from .identity import require_reviewer

APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
 run_id TEXT NOT NULL REFERENCES runs(id),
 status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','blocked')),
 created_at TEXT NOT NULL,
 customer_id TEXT NOT NULL REFERENCES customers(id),
 amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer' AND amount_cents>0),
 reviewed_by TEXT, decision_note TEXT NOT NULL DEFAULT '', reviewed_at TEXT,
 result_status TEXT, result_reason TEXT NOT NULL DEFAULT '');
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 product TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer' AND amount_cents>0),
 purchased_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('paid','refunded')));
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, request TEXT NOT NULL,
 model_mode TEXT NOT NULL, order_id TEXT,
 status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 UNIQUE(customer_id, request_key));
CREATE TABLE IF NOT EXISTS refunds(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
 amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer' AND amount_cents>0),
 provider TEXT NOT NULL CHECK(provider='fake'), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
 step TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(run_id, step));
"""


class Database:
    def __init__(self, path: str | Path, clock: Callable = utcnow):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        # WAL activation itself can return SQLITE_BUSY immediately on concurrent
        # legacy startup, before the normal transaction busy timeout applies.
        for attempt in range(20):
            try:
                with self.connection() as con:
                    con.execute("PRAGMA journal_mode=WAL")
                    con.executescript(SCHEMA + APPROVAL_SCHEMA)
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 19:
                    raise
                time.sleep(min(0.02 * 2**attempt, 0.25))
        self._migrate_approvals()

    def _migrate_approvals(self):
        """Rebuild the stage-1 pending-only table atomically, preserving its IDs."""
        with self.connection(write=True) as con:
            columns = {row["name"] for row in con.execute("PRAGMA table_info(approvals)")}
            if "reviewed_at" in columns:
                return
            con.execute(APPROVAL_SCHEMA.replace("approvals(", "approvals_next("))
            con.execute("""INSERT INTO approvals_next(id,order_id,run_id,status,created_at,
                           customer_id,amount_cents)
                           SELECT a.id,a.order_id,a.run_id,a.status,a.created_at,
                                  r.customer_id,o.amount_cents
                           FROM approvals a JOIN runs r ON r.id=a.run_id
                           JOIN orders o ON o.id=a.order_id""")
            con.execute("DROP TABLE approvals")
            con.execute("ALTER TABLE approvals_next RENAME TO approvals")

    @contextmanager
    def connection(self, write=False):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            if write:
                con.commit()
        except BaseException:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def seed(self, reset=False):
        with self.connection(write=True) as con:
            if reset:
                for table in ("events", "approvals", "refunds", "runs", "orders", "customers"):
                    con.execute(f"DELETE FROM {table}")
            if con.execute("SELECT 1 FROM customers").fetchone():
                return
            con.executemany("INSERT INTO customers VALUES (?,?)", [("C001", "김민지"), ("C002", "이준호")])
            fixtures = [
                ("ORD-001", "C001", "키보드 · 정상 환불", 4900, 5, "paid"),
                ("ORD-002", "C001", "마우스 · 기간 초과", 3500, 45, "paid"),
                ("ORD-003", "C001", "모니터 · 승인 필요", 15900, 3, "paid"),
                ("ORD-004", "C002", "헤드폰 · 다른 고객", 7900, 7, "paid"),
                ("ORD-005", "C001", "케이블 · 이미 환불", 1900, 2, "refunded"),
                ("ORD-006", "C001", "허브 · 정확히 100달러", 10000, 4, "paid"),
            ]
            now = self.clock()
            for oid, cid, product, cents, days, status in fixtures:
                con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)",
                            (oid, cid, product, cents, (now-timedelta(days=days)).isoformat(), status))
            # Seed refunded order with a matching historical ledger entry.
            con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                        ("seed-run", "C001", "seed", "seed", "과거 환불", "normal", "ORD-005",
                         "refunded", "초기 데이터의 과거 환불", now.isoformat()))
            con.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?)",
                        ("seed-refund", "ORD-005", "seed-run", 1900, "fake", now.isoformat()))

    def rows(self, table, **filters):
        # Table/column names are allowlisted; values always use placeholders.
        allowed = {"customers", "orders", "runs", "refunds", "approvals", "events"}
        if table not in allowed or any(k not in {"id", "customer_id", "run_id", "order_id"} for k in filters):
            raise ValueError("Unsupported query")
        where = " AND ".join(f"{k}=?" for k in filters)
        with self.connection() as con:
            return [dict(r) for r in con.execute(
                f"SELECT * FROM {table}" + (f" WHERE {where}" if where else ""), tuple(filters.values()))]

    def run(self, run_id):
        return self.rows("runs", id=run_id)[0]

    def claim(self, customer_id, key, request, mode):
        fingerprint = hashlib.sha256(json.dumps([request, mode], ensure_ascii=False).encode()).hexdigest()
        with self.connection(write=True) as con:
            if not con.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
                raise ValueError("알 수 없는 세션 고객입니다.")
            existing = con.execute("SELECT * FROM runs WHERE customer_id=? AND request_key=?", (customer_id, key)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ValueError("같은 요청 키를 다른 내용이나 모델 모드에 재사용할 수 없습니다.")
                return dict(existing), False
            rid = str(uuid.uuid4())
            con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, customer_id, key, fingerprint, request, mode, None, "running", "", self.clock().isoformat()))
            return dict(con.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()), True

    def event(self, run_id, step, payload):
        with self.connection(write=True) as con:
            self._event(con, run_id, step, payload)

    def _event(self, con, run_id, step, payload):
        con.execute("INSERT INTO events(run_id,step,payload,created_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(run_id,step) DO UPDATE SET payload=excluded.payload",
                    (run_id, step, json.dumps(payload, ensure_ascii=False), self.clock().isoformat()))

    def finish(self, run_id, status, reason, order_id=None):
        with self.connection(write=True) as con:
            # A stale workflow must never overwrite a committed refund or staff decision.
            con.execute("UPDATE runs SET status=?,reason=?,order_id=? WHERE id=? AND status IN ('running','failed','waiting_approval')",
                        (status, reason, order_id, run_id))

    def scoped_order(self, customer_id, order_id):
        rows = self.rows("orders", id=order_id, customer_id=customer_id)
        return rows[0] if rows else None

    def settle(self, run_id):
        """Only fake execution boundary; policy, payment record, and status are atomic."""
        with self.connection(write=True) as con:
            run = dict(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            # Covers retry after commit but before the graph checkpoint was written.
            refund = con.execute("SELECT * FROM refunds WHERE run_id=?", (run_id,)).fetchone()
            if refund:
                return {"status": "refunded", "reason": "가짜 환불이 완료되었습니다.", "refund_id": refund["id"]}
            approval = con.execute("SELECT * FROM approvals WHERE order_id=?", (run["order_id"],)).fetchone()
            if approval:
                # A new model request cannot cancel a human decision or bypass pending review.
                result = self._approval_result(con, dict(approval))
                con.execute("UPDATE runs SET status=?,reason=? WHERE id=?",
                            (result["status"], result["reason"], run_id))
                self._event(con, run_id, "execute", result)
                return result
            row = con.execute("SELECT * FROM orders WHERE id=?", (run["order_id"],)).fetchone()
            order = dict(row) if row else None
            status, reason = evaluate(order, run["customer_id"], self.clock())
            refund_id = None
            if status == "eligible":
                refund_id = str(uuid.uuid4())
                con.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?)", (
                    refund_id, order["id"], run_id, order["amount_cents"], "fake", self.clock().isoformat()))
                con.execute("UPDATE orders SET status='refunded' WHERE id=?", (order["id"],))
                status, reason = "refunded", "가짜 환불이 완료되었습니다. 실제 결제는 발생하지 않았습니다."
            elif status == "waiting_approval":
                con.execute("INSERT INTO approvals(id,order_id,run_id,status,created_at,customer_id,amount_cents) "
                            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(order_id) DO NOTHING",
                            (str(uuid.uuid4()), order["id"], run_id, "pending", self.clock().isoformat(),
                             run["customer_id"], order["amount_cents"]))
            result = {"status": status, "reason": reason, "refund_id": refund_id}
            con.execute("UPDATE runs SET status=?,reason=? WHERE id=?", (status, reason, run_id))
            self._event(con, run_id, "execute", result)
            return result

    def list_approvals(self, reviewer_id, pending_only=True):
        require_reviewer(reviewer_id)
        with self.connection() as con:
            return [dict(row) for row in con.execute(
                "SELECT a.*, c.name AS customer_name, o.product FROM approvals a "
                "JOIN customers c ON c.id=a.customer_id JOIN orders o ON o.id=a.order_id "
                + ("WHERE a.status='pending' " if pending_only else "")
                + "ORDER BY a.created_at, a.id")]

    def _approval_result(self, con, approval):
        refund = con.execute("SELECT id FROM refunds WHERE order_id=?", (approval["order_id"],)).fetchone()
        return {"approval_id": approval["id"], "approval_status": approval["status"],
                "status": approval["result_status"] or "waiting_approval",
                "reason": approval["result_reason"] or "사람 승인 대기입니다. 환불을 실행하지 않았습니다.",
                "refund_id": refund["id"] if refund else None,
                "reviewed_by": approval["reviewed_by"], "reviewed_at": approval["reviewed_at"],
                "decision_note": approval["decision_note"]}

    def review_result(self, approval_id, reviewer_id):
        require_reviewer(reviewer_id)
        with self.connection() as con:
            row = con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise ValueError("승인 요청을 찾을 수 없습니다.")
            return self._approval_result(con, dict(row))

    def review(self, approval_id, decision, reviewer_id, note=""):
        """First terminal decision wins. Validation, fake refund, and audit are atomic."""
        require_reviewer(reviewer_id)
        if decision not in ("approve", "reject"):
            raise ValueError("승인 또는 거절을 선택해주세요.")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("처리 메모는 1000자 이하여야 합니다.")
        with self.connection(write=True) as con:
            row = con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise ValueError("승인 요청을 찾을 수 없습니다.")
            approval = dict(row)
            if approval["status"] != "pending":
                return self._approval_result(con, approval)
            row = con.execute("SELECT * FROM orders WHERE id=?", (approval["order_id"],)).fetchone()
            order = dict(row) if row else None
            # This flag is only supplied here, after validating the staff session.
            policy_status, policy_reason = evaluate(order, approval["customer_id"], self.clock(),
                                                     human_approved=True)
            if order and order["amount_cents"] != approval["amount_cents"]:
                policy_status, policy_reason = "blocked", "승인 요청 당시와 주문 금액이 달라 환불을 차단했습니다."
            if con.execute("SELECT 1 FROM refunds WHERE order_id=?", (approval["order_id"],)).fetchone():
                policy_status, policy_reason = "blocked", "이미 환불된 주문입니다."
            if decision == "reject":
                approval_status, status, reason = "rejected", "rejected", "담당자가 환불을 거절했습니다."
            elif policy_status != "eligible":
                approval_status, status, reason = "blocked", "blocked", policy_reason
            else:
                approval_status, status = "approved", "refunded"
                reason = "사람 승인 후 서버 정책을 재검사하여 가짜 환불을 완료했습니다."
                con.execute("INSERT INTO refunds VALUES (?,?,?,?,?,?)",
                            (str(uuid.uuid4()), approval["order_id"], approval["run_id"],
                             order["amount_cents"], "fake", self.clock().isoformat()))
                con.execute("UPDATE orders SET status='refunded' WHERE id=?", (approval["order_id"],))
            con.execute("UPDATE approvals SET status=?,reviewed_by=?,decision_note=?,reviewed_at=?,"
                        "result_status=?,result_reason=? WHERE id=? AND status='pending'",
                        (approval_status, reviewer_id, note.strip(), self.clock().isoformat(),
                         status, reason, approval_id))
            approval = dict(con.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())
            result = self._approval_result(con, approval)
            # All requests sharing the same pending approval see the final decision.
            linked = con.execute("SELECT id FROM runs WHERE order_id=? AND customer_id=? "
                                 "AND (status IN ('running','waiting_approval') OR id=?)",
                                 (approval["order_id"], approval["customer_id"], approval["run_id"])).fetchall()
            for linked_run in linked:
                con.execute("UPDATE runs SET status=?,reason=? WHERE id=?", (status, reason, linked_run["id"]))
                self._event(con, linked_run["id"], "approval_policy",
                            {"status": policy_status, "reason": policy_reason,
                             "amount_cents": order["amount_cents"] if order else None})
                self._event(con, linked_run["id"], "human_decision", {**result, "decision": decision})
            return result


class LocalRefundTools:
    def __init__(self, db: Database):
        self.db = db

    def lookup(self, customer_id, order_id):
        return self.db.scoped_order(customer_id, order_id)

    def execute(self, run_id):
        return self.db.settle(run_id)
