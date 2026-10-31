"""Run with: streamlit run app.py"""
import json
import os
from pathlib import Path
import uuid

import streamlit as st

from refund_lab.db import Database
from refund_lab.service import RefundService
from refund_lab.identity import REVIEWERS
from refund_lab.i18n import translate, translate_tree


def tr(text):
    return translate(text, st.session_state.get("language", os.environ.get("REFUND_LAB_LANGUAGE", "en")))


def display(value):
    return translate_tree(value, st.session_state.get("language", "en"))

BASE = Path(__file__).parent
DATA = Path(os.environ.get("REFUND_LAB_DATA_DIR", str(BASE / "data")))
DATA.mkdir(parents=True, exist_ok=True)
db = Database(DATA / "lab.sqlite3")
db.seed()
service = RefundService(db, DATA / "checkpoints.sqlite3")
LABELS = {"running": tr("처리 중"), "refunded": tr("환불 완료"), "blocked": tr("차단됨"),
          "waiting_approval": tr("사람 승인 대기"), "needs_info": tr("추가 정보 필요"), "failed": tr("실행 실패"),
          "rejected": tr("환불 거절"), "pending": tr("승인 대기"), "approved": tr("승인 완료")}


def dollars(cents):
    return f"${cents // 100:,}.{cents % 100:02d}"


st.set_page_config(page_title="Refund Agent Lab", page_icon="🧪", layout="wide")
st.title("🧪 Refund Agent Lab")
st.caption(tr("2단계 · 사람 승인·거절 · API 키 없는 가짜 모델 · 실제 결제 없음 · 모든 금액은 정수 센트로 저장"))

with st.sidebar:
    st.selectbox(tr("Language / 언어"), ["en", "ko"],
                 index=0 if os.environ.get("REFUND_LAB_LANGUAGE", "en") == "en" else 1,
                 format_func=lambda lang: "English" if lang == "en" else tr("한국어"), key="language")
    st.header(tr("실험 설정"))
    customers = db.rows("customers")
    customer_names = {c["id"]: tr(c["name"]) for c in customers}
    st.selectbox(tr("현재 고객 (세션 신원)"), [c["id"] for c in customers],
                 format_func=customer_names.get, key="customer_id")
    customer_id = st.session_state.customer_id
    model_labels = {"normal": tr("일반 제안"), "reckless": tr("오류 실험: 환불 의사를 무시하고 제안")}
    mode = st.selectbox(tr("가짜 모델"), ["normal", "reckless"], format_func=model_labels.get)
    st.caption(tr("고객 선택은 학습용 로그인 대체입니다. 모델이 고객 신원을 결정하지 않습니다."))
    reviewer_mode = st.checkbox(tr("담당자 모드 (학습용)"), key="reviewer_mode")
    if reviewer_mode:
        reviewer_names = {sid: tr(name) for sid, name in REVIEWERS.items()}
        st.selectbox(tr("담당자 (세션 신원)"), list(REVIEWERS),
                     format_func=reviewer_names.get, key="reviewer_id")
        st.caption(tr("실제 인증이 없는 역할 시뮬레이션입니다. 담당자는 모든 고객의 승인 대기를 처리합니다."))
    st.info(tr("정책: 30일 이내 · $100 초과 승인 필요 · 본인 주문 · 중복 환불 금지"))
    st.caption(tr("초기화하면 모든 고객의 실험 기록을 지우고 샘플을 다시 만듭니다."))
    if st.button(tr("실험 데이터 초기화"), type="secondary"):
        db.seed(reset=True)
        for key in ("last_submission", "active_run", "review_message"):
            st.session_state.pop(key, None)
        st.rerun()

if reviewer_mode:
    st.subheader(tr("사람 승인 대기 목록"))
    message = st.session_state.pop("review_message", None)
    if message:
        st.info(tr(message))
    reviewer_id = st.session_state.reviewer_id
    pending = db.list_approvals(reviewer_id)
    st.caption(tr("승인은 금액 제한만 해제합니다. 소유권·30일 기한·기존 환불·요청 당시 금액을 서버에서 다시 검사합니다."))
    if st.button(tr("승인 목록 새로고침")):
        st.rerun()
    if not pending:
        st.info(tr("현재 승인 대기 건이 없습니다."))
    else:
        st.dataframe([{tr("주문"): a["order_id"], tr("고객"): tr(a["customer_name"]),
                       tr("상품"): tr(a["product"]), tr("요청 금액"): dollars(a["amount_cents"]),
                       tr("대기 시작 (UTC)"): a["created_at"]} for a in pending],
                     hide_index=True, width="stretch")
        approval_ids = [a["id"] for a in pending]
        approval_labels = {a["id"]: f"{a['order_id']} · {tr(a['customer_name'])} · {dollars(a['amount_cents'])}"
                           for a in pending}
        aid = st.selectbox(tr("처리할 승인 요청"), approval_ids, format_func=approval_labels.get)
        selected = next(a for a in pending if a["id"] == aid)
        st.caption(tr("승인 요청 ID: ") + aid)
        with st.form(f"review_{aid}"):
            note = st.text_area(tr("처리 메모 (선택)"), max_chars=1000)
            approve = st.form_submit_button(tr("승인 후 환불"), type="primary")
            reject = st.form_submit_button(tr("거절"))
        if approve or reject:
            try:
                result = service.review(aid, "approve" if approve else "reject", reviewer_id, note)
                st.session_state.review_message = result["reason"]
                if selected["customer_id"] == customer_id:
                    st.session_state.active_run = selected["run_id"]
                st.rerun()
            except (ValueError, RuntimeError) as exc:
                st.error(tr(str(exc)))
    with st.expander(tr("승인·거절 처리 이력"), expanded=True):
        history = [a for a in db.list_approvals(reviewer_id, pending_only=False) if a["status"] != "pending"]
        if history:
            st.dataframe([{tr("주문"): a["order_id"], tr("고객"): tr(a["customer_name"]),
                           tr("금액"): dollars(a["amount_cents"]), tr("승인 상태"): LABELS[a["status"]],
                           tr("최종 상태"): LABELS[a["result_status"]],
                           tr("담당자"): tr(REVIEWERS.get(a["reviewed_by"], a["reviewed_by"])),
                           tr("처리 시각 (UTC)"): a["reviewed_at"], tr("메모"): a["decision_note"],
                           tr("결과 근거"): tr(a["result_reason"])} for a in history],
                         hide_index=True, width="stretch")
        else:
            st.caption(tr("아직 처리된 승인 요청이 없습니다."))
    st.divider()

left, right = st.columns([1, 1])
with left:
    st.subheader(tr("고객 요청"))
    with st.form("refund_request"):
        request = st.text_area(tr("요청 입력"), value=tr("ORD-001 주문을 환불해주세요."), max_chars=4000,
                               help=tr("주문 번호 하나와 환불 의사를 입력하세요. 모호하면 확인을 요청합니다."))
        submitted = st.form_submit_button(tr("요청 처리"), type="primary")
    if submitted:
        key = str(uuid.uuid4())
        try:
            result = service.submit(customer_id, request, key, mode)
            st.session_state.last_submission = (customer_id, request, key, mode)
            st.session_state.active_run = result["id"]
        except ValueError as exc:
            st.error(tr(str(exc)))
    previous = st.session_state.get("last_submission")
    if previous and previous[0] == customer_id:
        if st.button(tr("같은 요청 다시 보내기 (중복 실험)")):
            cid, text, key, old_mode = previous
            result = service.submit(cid, text, key, old_mode)
            st.session_state.active_run = result["id"]
            st.info(tr("같은 요청 키로 저장된 결과를 반환했습니다."))
    st.subheader(tr("현재 고객의 주문"))
    orders = db.rows("orders", customer_id=customer_id)
    st.dataframe([{tr("주문"): o["id"], tr("상품 / 시나리오"): tr(o["product"]), tr("금액"): dollars(o["amount_cents"]),
                   tr("구매 시각 (UTC)"): o["purchased_at"], tr("상태"): o["status"]} for o in orders], hide_index=True, width="stretch")
    st.caption(tr("다른 고객 소유 차단 실험: 김민지 선택 → ORD-004 주문을 환불해주세요."))
    st.caption(tr("정보 부족 실험: ‘환불해주세요’ / 오류 모델 실험: ‘ORD-002 상태를 알려주세요’"))

with right:
    st.subheader(tr("판단과 실행 기록"))
    runs = sorted(db.rows("runs", customer_id=customer_id), key=lambda r: r["created_at"], reverse=True)
    if not runs:
        st.info(tr("요청을 처리하면 단계별 기록이 여기에 나타납니다."))
    else:
        ids = [r["id"] for r in runs]
        active = st.session_state.get("active_run")
        rid = st.selectbox(tr("실행 선택"), ids, index=ids.index(active) if active in ids else 0,
                           format_func=lambda k: f"{LABELS.get(db.run(k)['status'], db.run(k)['status'])} · {db.run(k)['request'][:40]} · {k[:8]}")
        run = db.run(rid)
        status = run["status"]
        st.metric(tr("최종 처리 상태"), LABELS.get(status, status))
        st.write(tr(run["reason"]) or tr("다른 요청 처리 중일 수 있습니다. 화면을 새로고침하세요."))
        st.caption(tr("실행 ID: ") + rid)
        if status == "waiting_approval":
            st.warning(tr("승인 대기만 저장되었습니다. 사이드바의 담당자 모드에서 승인·거절할 수 있습니다. 승인 전에는 환불되지 않습니다."))
        if status == "failed" and st.button(tr("실패한 실행 재시도")):
            service.retry_failed(customer_id, rid)
            st.session_state.active_run = rid
            st.rerun()
        events = sorted(db.rows("events", run_id=rid), key=lambda e: e["id"])
        names = {"propose": tr("1. 모델의 제안"), "lookup": tr("2. 본인 주문 조회"), "policy": tr("3. 서버 정책 검사"),
                 "execute": tr("4. 트랜잭션에서 재검사 및 가짜 실행"), "finalize": tr("5. 요청 처리 상태 저장"), "error": tr("오류 기록"),
                 "approval_policy": tr("6. 승인 시 서버 정책 재검사"), "human_decision": tr("7. 사람의 결정과 최종 결과")}
        for event in events:
            with st.expander(names.get(event["step"], event["step"]), expanded=True):
                st.json(display(json.loads(event["payload"])))
        with st.expander(tr("DB 기록: 환불 / 승인 대기")):
            st.write(tr("이 주문의 환불"), display(db.rows("refunds", order_id=run["order_id"])) if run["order_id"] else [])
            st.write(tr("이 주문의 승인 대기"), display(db.rows("approvals", order_id=run["order_id"])) if run["order_id"] else [])

st.divider()
st.caption(tr("모델 제안 → 본인 주문 조회 → 서버 정책 → 실행 직전 재검사 → 가짜 환불/승인 대기 → 담당자 결정 → 정책 재검사 → 가짜 환불/거절 → 기록. 업무 DB와 LangGraph 체크포인트는 디스크에 저장됩니다."))
