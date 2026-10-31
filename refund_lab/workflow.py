"""Small, durable LangGraph workflow; the ledger remains the payment authority."""
import re
from typing import Any, TypedDict

from langgraph.graph import StateGraph, START, END

from .adapters import ModelAdapter, NullObserver, Observer, RefundTools
from .db import Database
from .policy import evaluate


class State(TypedDict, total=False):
    run_id: str
    proposal: dict[str, Any]
    order: dict[str, Any] | None
    status: str
    reason: str
    refund_id: str | None


def build_graph(db: Database, model: ModelAdapter, tools: RefundTools,
                checkpointer, observer: Observer | None = None):
    observer = observer or NullObserver()

    def record(state, step, payload):
        db.event(state["run_id"], step, payload)
        try:
            observer.on_step(state["run_id"], step, payload)
        except Exception:
            # Telemetry cannot change the outcome of a financial operation.
            pass

    def propose(state):
        run = db.run(state["run_id"])
        raw = model.propose(run["request"])
        # Treat even a real model's structured response as untrusted input.
        if (not isinstance(raw, dict) or raw.get("action") not in ("refund", "clarify")
                or not isinstance(raw.get("reason"), str)
                or (raw.get("action") == "refund" and
                    (not isinstance(raw.get("order_id"), str)
                     or not re.fullmatch(r"ORD-\d{3}", raw["order_id"])) )):
            raw = {"action": "clarify", "order_id": None, "reason": "유효한 주문 번호와 환불 의사를 확인해주세요."}
        # Strip all unexpected fields, including customer IDs and amounts.
        proposal = {k: raw.get(k) for k in ("action", "order_id", "reason")}
        record(state, "propose", proposal)
        return {"proposal": proposal}

    def lookup(state):
        run = db.run(state["run_id"])
        p = state["proposal"]
        order = tools.lookup(run["customer_id"], p["order_id"]) if p["action"] == "refund" else None
        db.finish(run["id"], "running", "", p["order_id"] if p["action"] == "refund" else None)
        record(state, "lookup", {"order": order})
        return {"order": order}

    def policy(state):
        run = db.run(state["run_id"])
        p = state["proposal"]
        status, reason = (("needs_info", p["reason"]) if p["action"] != "refund"
                          else evaluate(state["order"], run["customer_id"], db.clock()))
        record(state, "policy", {"status": status, "reason": reason})
        return {"status": status, "reason": reason}

    def execute(state):
        # settle() re-reads the actual order and policy under BEGIN IMMEDIATE.
        result = tools.execute(state["run_id"])
        record(state, "execute", result)
        return result

    def finalize(state):
        db.finish(state["run_id"], state["status"], state["reason"], db.run(state["run_id"])["order_id"])
        # A staff decision may commit between execute and this finalizer.
        current = db.run(state["run_id"])
        result = {"status": current["status"], "reason": current["reason"]}
        record(state, "finalize", result)
        return result

    graph = StateGraph(State)
    for name, node in (("propose", propose), ("lookup", lookup), ("policy", policy),
                       ("execute", execute), ("finalize", finalize)):
        graph.add_node(name, node)
    graph.add_edge(START, "propose")
    graph.add_edge("propose", "lookup")
    graph.add_edge("lookup", "policy")
    graph.add_conditional_edges("policy", lambda s: "execute" if s["status"] in
                               ("eligible", "waiting_approval") else "finalize")
    graph.add_edge("execute", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


class ReviewState(TypedDict, total=False):
    approval_id: str
    decision: str
    reviewer_id: str
    note: str
    result: dict[str, Any]


def build_review_graph(db: Database, checkpointer, observer: Observer | None = None):
    """Staff decisions have their own durable graph; never invoke the model here."""
    observer = observer or NullObserver()

    def decide(state):
        return {"result": db.review(state["approval_id"], state["decision"],
                                    state["reviewer_id"], state.get("note", ""))}

    def observe(state):
        try:
            approval = db.rows("approvals", id=state["approval_id"])[0]
            observer.on_step(approval["run_id"], "human_decision", state["result"])
        except Exception:
            pass
        return {}

    graph = StateGraph(ReviewState)
    graph.add_node("decide", decide)
    graph.add_node("observe", observe)
    graph.add_edge(START, "decide")
    graph.add_edge("decide", "observe")
    graph.add_edge("observe", END)
    return graph.compile(checkpointer=checkpointer)
