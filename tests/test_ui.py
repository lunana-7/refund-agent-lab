from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

from refund_lab.db import Database

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture(autouse=True)
def korean_ui(monkeypatch):
    monkeypatch.setenv("REFUND_LAB_LANGUAGE", "ko")


def test_screen_refund_duplicate_and_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_LAB_DATA_DIR", str(tmp_path))
    at = AppTest.from_file(APP).run(timeout=20)
    assert not at.exception
    next(b for b in at.button if b.label == "요청 처리").click().run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == "환불 완료"
    db = Database(tmp_path / "lab.sqlite3")
    assert len(db.rows("refunds", order_id="ORD-001")) == 1
    next(b for b in at.button if "같은 요청" in b.label).click().run(timeout=20)
    assert not at.exception
    assert len(db.rows("refunds", order_id="ORD-001")) == 1
    next(b for b in at.button if b.label == "실험 데이터 초기화").click().run(timeout=20)
    assert not at.exception
    assert not db.rows("refunds", order_id="ORD-001")


def test_screen_pending_and_customer_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_LAB_DATA_DIR", str(tmp_path))
    at = AppTest.from_file(APP).run(timeout=20)
    at.text_area[0].set_value("ORD-003 환불해주세요")
    next(b for b in at.button if b.label == "요청 처리").click().run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == "사람 승인 대기"
    db = Database(tmp_path / "lab.sqlite3")
    assert not db.rows("refunds", order_id="ORD-003")
    next(s for s in at.selectbox if s.label.startswith("현재 고객")).select("C002").run(timeout=20)
    assert not at.exception
    assert not at.metric  # previous customer's run is not shown


def test_screen_staff_approve_and_pending_disappears(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_LAB_DATA_DIR", str(tmp_path))
    at = AppTest.from_file(APP).run(timeout=20)
    at.text_area[0].set_value("ORD-003 환불해주세요")
    next(b for b in at.button if b.label == "요청 처리").click().run(timeout=20)
    assert at.metric[0].value == "사람 승인 대기"
    assert not any(b.label == "승인 후 환불" for b in at.button)
    at.checkbox[0].check().run(timeout=20)
    assert not at.exception
    next(t for t in at.text_area if t.label == "처리 메모 (선택)").set_value("화면에서 승인")
    next(b for b in at.button if b.label == "승인 후 환불").click().run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == "환불 완료"
    assert not any(b.label == "승인 후 환불" for b in at.button)
    db = Database(tmp_path / "lab.sqlite3")
    assert len(db.rows("refunds", order_id="ORD-003")) == 1
    assert db.rows("approvals")[0]["decision_note"] == "화면에서 승인"
    next(b for b in at.button if "같은 요청" in b.label).click().run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == "환불 완료"
    assert len(db.rows("refunds", order_id="ORD-003")) == 1


def test_screen_staff_reject_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_LAB_DATA_DIR", str(tmp_path))
    at = AppTest.from_file(APP).run(timeout=20)
    at.text_area[0].set_value("ORD-003 환불해주세요")
    next(b for b in at.button if b.label == "요청 처리").click().run(timeout=20)
    at.checkbox[0].check().run(timeout=20)
    next(b for b in at.button if b.label == "거절").click().run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == "환불 거절"
    db = Database(tmp_path / "lab.sqlite3")
    assert db.rows("approvals")[0]["status"] == "rejected"
    assert not db.rows("refunds", order_id="ORD-003")
    at.checkbox[0].uncheck().run(timeout=20)
    assert not at.exception
    assert not any(e.label == "승인·거절 처리 이력" for e in at.expander)
