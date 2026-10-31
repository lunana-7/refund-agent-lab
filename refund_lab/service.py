from pathlib import Path
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver

from .adapters import FakeModel, ModelAdapter, Observer
from .db import Database, LocalRefundTools
from .workflow import build_graph, build_review_graph
from .identity import require_reviewer


class RefundService:
    def __init__(self, db: Database, checkpoint_path: str | Path,
                 model: ModelAdapter | None = None, observer: Observer | None = None):
        self.db = db
        self.checkpoint_path = str(checkpoint_path)
        self.model = model
        self.observer = observer

    def submit(self, customer_id: str, request: str, request_key: str,
               mode: str = "normal"):
        if mode not in ("normal", "reckless"):
            raise ValueError("지원하지 않는 모델 모드입니다.")
        if not request.strip() or len(request) > 4000 or not request_key or len(request_key) > 200:
            raise ValueError("요청(1~4000자)과 요청 키(1~200자)가 필요합니다.")
        run, claimed = self.db.claim(customer_id, request_key, request.strip(), mode)
        if claimed:
            self._invoke(run, resume=False)
        # Same request in flight is returned as running; only its owner invokes it.
        return self.db.run(run["id"])

    def retry_failed(self, customer_id: str, run_id: str):
        """Resume a caught failure from the persisted checkpoint, with a DB claim."""
        with self.db.connection(write=True) as con:
            row = con.execute("SELECT * FROM runs WHERE id=? AND customer_id=?", (run_id, customer_id)).fetchone()
            if row is None:
                raise ValueError("현재 고객의 실행 기록이 아닙니다.")
            claimed = con.execute("UPDATE runs SET status='running' WHERE id=? AND status='failed'", (run_id,)).rowcount
        if claimed:
            self._invoke(dict(row), resume=True)
        return self.db.run(run_id)

    def review(self, approval_id: str, decision: str, reviewer_id: str, note: str = ""):
        require_reviewer(reviewer_id)
        try:
            with SqliteSaver.from_conn_string(self.checkpoint_path) as saver:
                graph = build_review_graph(self.db, saver, self.observer)
                # Different button presses use separate traces. The DB serializes the decision.
                output = graph.invoke({"approval_id": approval_id, "decision": decision,
                                       "reviewer_id": reviewer_id, "note": note},
                                      {"configurable": {"thread_id": f"review:{uuid.uuid4()}"}})
                return output["result"]
        except ValueError:
            raise
        except Exception as exc:
            # A checkpoint write may fail AFTER the business transaction committed.
            result = self.db.review_result(approval_id, reviewer_id)
            if result["approval_status"] != "pending":
                return result
            raise RuntimeError("승인 처리를 완료하지 못했습니다. 대기 상태를 유지했으니 다시 시도해주세요.") from exc

    def _invoke(self, run, resume):
        try:
            with SqliteSaver.from_conn_string(self.checkpoint_path) as saver:
                graph = build_graph(self.db, self.model or FakeModel(run["model_mode"] == "reckless"),
                                    LocalRefundTools(self.db), saver, self.observer)
                config = {"configurable": {"thread_id": run["id"]}}
                snapshot = graph.get_state(config) if resume else None
                graph.invoke(None if snapshot and snapshot.next else {"run_id": run["id"]}, config)
        except Exception as exc:
            # Don't expose potentially sensitive adapter exception text to the UI.
            self.db.event(run["id"], "error", {"type": type(exc).__name__, "message": "처리 중 오류가 발생했습니다. 실패한 실행을 재시도할 수 있습니다."})
            with self.db.connection(write=True) as con:
                con.execute("UPDATE runs SET status='failed',reason=? WHERE id=? AND status='running'",
                            ("처리 오류: 실행 기록을 확인하고 재시도해주세요.", run["id"]))
