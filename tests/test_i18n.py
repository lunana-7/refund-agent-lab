import ast
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from refund_lab.db import Database
from refund_lab.i18n import EN, translate, translate_tree

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def english_app(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_LAB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REFUND_LAB_LANGUAGE", "en")
    at = AppTest.from_file(APP).run(timeout=20)
    assert not at.exception
    return at, Database(tmp_path / "lab.sqlite3")


@pytest.mark.parametrize("text,status", [
    ("Please refund ORD-001.", "Refund completed"),
    ("Please refund ORD-002.", "Blocked"),
    ("Please refund ORD-003.", "Awaiting human approval"),
    ("Please refund ORD-004.", "Blocked"),
    ("Please refund.", "More information needed"),
    ("Can I refund ORD-001?", "More information needed"),
    ("Do not refund ORD-001.", "More information needed"),
    ("Please refund ORD-001 and ORD-006.", "More information needed"),
])
def test_english_customer_outcomes(english_app, text, status):
    at, _ = english_app
    next(t for t in at.text_area if t.label == "Request text").set_value(text)
    next(b for b in at.button if b.label == "Process request").click().run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == status
    # System explanations are translated; raw IDs and request text stay intact.
    for markdown in at.markdown:
        assert not any('\uac00' <= char <= '\ud7a3' for char in markdown.value)


@pytest.mark.parametrize("approve", [True, False])
def test_english_review_controls(english_app, approve):
    at, db = english_app
    next(t for t in at.text_area if t.label == "Request text").set_value("Please refund ORD-003.")
    next(b for b in at.button if b.label == "Process request").click().run(timeout=20)
    at.checkbox[0].check().run(timeout=20)
    assert not at.exception
    assert any(h.value == "Pending human approvals" for h in at.subheader)
    next(t for t in at.text_area if t.label == "Review note (optional)").set_value("Reviewed in English")
    label = "Approve and refund" if approve else "Reject"
    next(b for b in at.button if b.label == label).click().run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == ("Refund completed" if approve else "Refund rejected")
    assert len(db.rows("refunds", order_id="ORD-003")) == (1 if approve else 0)
    assert db.rows("approvals")[0]["decision_note"] == "Reviewed in English"
    assert any(e.label == "Approval and rejection history" for e in at.expander)


def test_language_switch_does_not_repeat_refund(english_app):
    at, db = english_app
    next(b for b in at.button if b.label == "Process request").click().run(timeout=20)
    assert at.metric[0].value == "Refund completed"
    original = db.rows("refunds", order_id="ORD-001")
    next(s for s in at.selectbox if s.label == "Language / 언어").select("ko").run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == "환불 완료"
    assert at.session_state["customer_id"] == "C001"
    next(s for s in at.selectbox if s.label == "Language / 언어").select("en").run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == "Refund completed"
    assert db.rows("refunds", order_id="ORD-001") == original
    next(b for b in at.button if b.label.startswith("Resend same request")).click().run(timeout=20)
    assert not at.exception
    assert db.rows("refunds", order_id="ORD-001") == original


def test_translation_is_presentation_only():
    original = {"amount_cents": 15900, "customer_id": "C001",
                "reason": "이미 환불된 주문입니다.", "note": "Customer-authored text"}
    english = translate_tree([original], "en")[0]
    assert english["reason"] == "This order has already been refunded."
    assert original["reason"] == "이미 환불된 주문입니다."
    assert english["amount_cents"] == original["amount_cents"]
    assert english["note"] == original["note"]
    assert translate("이미 환불된 주문입니다.", "ko") == original["reason"]


def test_all_literal_app_labels_have_english_translations():
    tree = ast.parse(Path(APP).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr":
            if node.args and isinstance(node.args[0], ast.Constant):
                text = node.args[0].value
                if any('\uac00' <= char <= '\ud7a3' for char in text):
                    assert text in EN or text in {"Language / 언어", "한국어"}, text
