"""Session-side demo staff identities. Replace with real auth before deployment."""
REVIEWERS = {"STAFF-001": "상담 담당자 A", "STAFF-002": "상담 담당자 B"}


def require_reviewer(reviewer_id: str) -> None:
    if reviewer_id not in REVIEWERS:
        raise ValueError("승인·거절은 담당자 세션에서만 처리할 수 있습니다.")
