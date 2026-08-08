"""`/auth/ask` 라우트 검사 — 안내원이 주소로 열리는 자리.

여기서 지키는 것은 **틀리면 홈페이지 전체가 다치는 자리들**이다. 안내원은
말풍선 하나지만 이 주소는 로그인 없이 누구나 두드릴 수 있어서, 새는 곳이
생기면 그 피해가 말풍선 안에서 끝나지 않는다.

- 로그인 없이 되는가 (되어야 한다 — 처음 온 사람이 쓰는 창구다)
- GET으로는 안 되는가 (링크 프리페치·미리보기 크롤러가 하루 몫을 쓴다)
- 큰 본문을 통째로 삼키지 않는가
- **어떤 사연에도 500이 아닌가** (말풍선 하나 때문에 오류 화면을 보면 안 된다)
- 방문자 쿠키가 로그인 쿠키와 섞이지 않는가
"""
import json

import ask
import pytest
import web_auth
from starlette.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """열쇠가 있고 AI는 가짜인 서버 한 대."""
    monkeypatch.setenv("GEMINI_API_KEY", "테스트열쇠")
    monkeypatch.setenv("NAMU_ASK_PROVIDER", "gemini")
    monkeypatch.setenv("NAMU_SESSION_SECRET", "테스트서명키")
    monkeypatch.setenv("NAMU_ASK_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        ask, "_PROVIDERS", {"gemini": lambda s, u, **kw: "답입니다.\n근거: [1]"}
    )
    # 안내원 한 벌은 한 번 만들면 계속 쓰이므로, 검사마다 새로 만들게 비운다.
    monkeypatch.setattr(ask, "_guide", None)
    # `https://`로 부른다 — 쿠키가 secure=True라 http로는 브라우저(와 검사용
    # 손님)가 되돌려 보내지 않는다. 이 저장소의 다른 웹 검사와 같은 방식이다.
    return TestClient(web_auth.build_auth_app(), base_url="https://testserver")


def ask_post(client, question="무료인가요", **kw):
    return client.post("/auth/ask", json={"question": question, **kw})


# ---------------------------------------------------------------------------
# 누가 쓸 수 있나
# ---------------------------------------------------------------------------
def test_works_without_logging_in(client):
    """사용자 결정(설계서 2절) — 로그인 전에도 누구나. 이 파일의 다른 POST와
    달리 세션을 요구하지 않는 유일한 자리다."""
    resp = ask_post(client)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["text"] == "답입니다."


def test_get_is_refused(client):
    """링크 프리페치나 채팅 미리보기 크롤러가 눌러 하루 몫을 대신 쓰는 것을
    방법(method) 단계에서 막는다."""
    assert client.get("/auth/ask").status_code == 405


def test_answer_carries_source_links(client):
    body = ask_post(client).json()

    assert body["sources"], "근거 링크가 빠졌다"
    assert all(s["url"].startswith("https://") for s in body["sources"])


# ---------------------------------------------------------------------------
# 방문자 쿠키 — 로그인 쿠키와 섞이면 안 된다
# ---------------------------------------------------------------------------
def test_first_question_hands_out_a_visitor_cookie(client):
    resp = ask_post(client)

    assert web_auth._ASK_COOKIE_NAME in resp.cookies


def test_visitor_cookie_is_not_the_login_cookie(client):
    """이름이나 경로가 겹치면 한쪽을 지울 때 다른 쪽이 함께 사라진다."""
    assert web_auth._ASK_COOKIE_NAME != web_auth._SESSION_COOKIE_NAME


def test_visitor_cookie_is_signed(client):
    """서명이 없으면 아무 값이나 적어 넣어 한도를 새로 시작할 수 있다."""
    raw = ask_post(client).cookies[web_auth._ASK_COOKIE_NAME]

    assert web_auth._unsign_with_expiry(raw), "서명이 확인되지 않는 쿠키"
    assert web_auth._unsign_with_expiry("아무값") is None


def test_the_same_cookie_is_not_handed_out_twice(client):
    """이미 가진 사람에게 다시 발급하면 세던 값이 매번 0으로 돌아간다."""
    ask_post(client)  # 쿠키를 받아 둔다(TestClient가 들고 있는다)
    resp = ask_post(client, question="나무가 뭔가요")

    assert web_auth._ASK_COOKIE_NAME not in resp.cookies


# ---------------------------------------------------------------------------
# 접속 주소 — 터널 뒤라 그냥 읽으면 전원이 한 사람이 된다
# ---------------------------------------------------------------------------
def test_client_ip_prefers_the_forwarded_visitor(client):
    """`request.client.host`는 터널의 주소다. 그대로 세면 방문자 전원이 한
    사람으로 뭉쳐 사람별 한도가 사이트 전체 한도처럼 굳는다."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1")],
        "client": ("10.0.0.1", 5000),
    }

    assert web_auth._ask_client_ip(Request(scope)) == "1.2.3.4"


def test_client_ip_falls_back_to_the_connection(client):
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "client": ("10.0.0.1", 5000)}

    assert web_auth._ask_client_ip(Request(scope)) == "10.0.0.1"


# ---------------------------------------------------------------------------
# 방문자가 보내는 것을 믿지 않는다
# ---------------------------------------------------------------------------
def test_oversized_body_is_refused_without_reading_it(client):
    resp = client.post(
        "/auth/ask",
        content=json.dumps({"question": "가" * 50000}),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["reason"] == "too_long"


def test_broken_body_is_a_400_not_a_500(client):
    resp = client.post(
        "/auth/ask",
        content="{이건 JSON이 아니다".encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    assert resp.status_code == 400


def test_a_bare_list_body_does_not_crash(client):
    resp = client.post("/auth/ask", json=["질문"])

    assert resp.status_code == 400


def test_missing_question_is_answered_not_crashed(client):
    resp = client.post("/auth/ask", json={})

    assert resp.status_code == 200
    assert resp.json()["reason"] == "empty"


def test_malformed_history_is_dropped_quietly(client):
    """대화 한 줄이 이상하다고 화면이 멈추면 안 된다 — 그 값은 전부 방문자가
    고쳐 보낼 수 있는 글이다."""
    resp = ask_post(client, history=["망가진 줄", {"q": "지난 질문", "a": "지난 답"}, 42])

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_history_shapes_both_accepted():
    assert web_auth._ask_history([{"q": "질문", "a": "답"}]) == [("질문", "답")]
    assert web_auth._ask_history([["질문", "답"]]) == [("질문", "답")]
    assert web_auth._ask_history("목록이 아님") == []


def test_history_is_capped_at_three_turns():
    raw = [{"q": f"질문{i}", "a": f"답{i}"} for i in range(10)]

    assert len(web_auth._ask_history(raw)) == ask.HISTORY_TURNS


# ---------------------------------------------------------------------------
# 실패해도 홈페이지는 멀쩡하다
# ---------------------------------------------------------------------------
def test_ai_failure_is_still_a_200(client, monkeypatch):
    """말풍선이 못 답하는 것과 홈페이지가 고장 난 것은 다른 일이다."""
    def boom(*a, **kw):
        raise RuntimeError("터졌다")

    monkeypatch.setattr(ask, "_PROVIDERS", {"gemini": boom})

    resp = ask_post(client)

    assert resp.status_code == 200
    assert resp.json()["reason"] == "provider_error"


def test_no_key_is_still_a_200(client, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    resp = ask_post(client)

    assert resp.status_code == 200
    assert resp.json()["reason"] == "disabled"
