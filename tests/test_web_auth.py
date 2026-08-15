"""web_auth.py 유닛 테스트 — GitHub 사용자 로그인(OAuth) 흐름.

네트워크는 `web_auth._http_json` 하나만 monkeypatch해 걷어낸다(github_app 테스트와
동일한 원칙). identity 커넥션은 `NAMU_IDENTITY_DB_PATH`를 tmp_path 아래로 돌려
파일 기반으로 검증한다(select-repo가 콜백과 별개 요청이라 `:memory:`를 공유할
수 없다).

이 파일의 단언은 되도록 **외부(GitHub) 계약값을 리터럴로**, **내부 구현
디테일(서명 등)은 실제 응답에서 추출한 값으로 왕복 검증**하는 방식을 쓴다 —
`wa._SOME_CONST`를 기대값으로 그대로 되읽는 자기참조 단언은 상수를 바꿔도
테스트가 함께 끌려가 아무것도 못 잡는다(오케스트레이터 지시사항).
"""
import asyncio
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

import identity
import routing_server as rs
import ui
import web_auth as wa


# ---------------------------------------------------------------------------
# 공통 픽스처
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_SESSION_SECRET", "test-session-secret-value")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_ID", "Iv1.testclientid")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_SECRET", "test-client-secret-marker-9f2a")
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))
    yield


@pytest.fixture
def client():
    # https:// base_url이 필요하다 — Secure 쿠키는 http:// TestClient 세션에서는
    # 저장되지 않아(실측 확인) 왕복 검증(login→callback→select-repo)이 끊긴다.
    return TestClient(wa.build_auth_app(), base_url="https://testserver")


def _make_fake_http_json(
    *,
    token_error=None,
    token_error_include_access_token=False,
    github_id=1001,
    login_name="octocat",
    repos=None,
    repos_total_count=None,
    installations=None,
    repos_by_installation=None,
):
    """web_auth._http_json 대역. 호출 인자를 기록해 나중에 검증할 수 있게 한다.

    installation 저장소 목록은 실제 GitHub처럼 `page`/`per_page` 쿼리에 따라
    슬라이스해 돌려주고 `total_count`를 함께 실어야 한다 — 그래야 "정말
    페이지네이션 전체를 따라갔는지"(2차 검수 지적 ②)를 검증할 수 있다.
    """
    calls = []
    all_repos = repos or []

    def _fake(method, url, *, headers=None, json_body=None):
        calls.append({"method": method, "url": url, "headers": headers, "json_body": json_body})
        if url == "https://github.com/login/oauth/access_token":
            if token_error:
                body = {"error": token_error, "error_description": "bad code"}
                if token_error_include_access_token:
                    # 실사용 GitHub에서 error와 access_token이 동시에 오지는
                    # 않지만, "error 필드를 실제로 봤는지"를 access_token
                    # 유무와 독립적으로 시험하기 위한 테스트 전용 조합이다.
                    body["access_token"] = "should-not-be-trusted"
                return 200, body
            return 200, {"access_token": "user-token-abcdef", "token_type": "bearer", "scope": ""}
        if url == "https://api.github.com/user":
            return 200, {"id": github_id, "login": login_name}
        if url.startswith("https://api.github.com/user/installations?"):
            # 설치 목록 조회(`GET /user/installations`) — installation_id 없이
            # 돌아온 콜백이 기존 설치를 찾을 때 쓴다. 기본값은 빈 목록이라
            # "설치 안 함" 흐름이 그대로 유지된다.
            qs = parse_qs(urlparse(url).query)
            page = int(qs.get("page", ["1"])[0])
            per_page = int(qs.get("per_page", ["30"])[0])
            start = (page - 1) * per_page
            all_installs = installations or []
            page_items = all_installs[start : start + per_page]
            return 200, {
                "total_count": len(all_installs),
                "installations": [{"id": i} for i in page_items],
            }
        if url.startswith("https://api.github.com/user/installations/"):
            qs = parse_qs(urlparse(url).query)
            page = int(qs.get("page", ["1"])[0])
            per_page = int(qs.get("per_page", ["30"])[0])
            start = (page - 1) * per_page
            if repos_by_installation is not None:
                iid = int(urlparse(url).path.split("/")[3])
                all_repos_here = repos_by_installation.get(iid, [])
            else:
                all_repos_here = all_repos
            page_items = all_repos_here[start : start + per_page]
            total = repos_total_count if repos_total_count is not None else len(all_repos_here)
            return 200, {
                "total_count": total,
                "repositories": [{"full_name": r} for r in page_items],
            }
        raise AssertionError(f"테스트가 예상하지 못한 URL 호출: {url}")

    return _fake, calls


def _extract_state(location: str) -> str:
    qs = parse_qs(urlparse(location).query)
    return qs["state"][0]


def _do_login(client) -> str:
    """login을 밟아 state 쿠키를 클라이언트 세션에 심고, 발급된 state 값을 돌려준다."""
    r = client.get("/auth/github/login", follow_redirects=False)
    assert r.status_code == 302
    return _extract_state(r.headers["location"])


# ---------------------------------------------------------------------------
# /auth/github/login
# ---------------------------------------------------------------------------
def test_login_redirects_to_github_authorize_without_redirect_uri(client):
    r = client.get("/auth/github/login", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    # 외부(GitHub) 계약값 — authorize 엔드포인트 경로는 리터럴로 못 박는다.
    assert location.startswith("https://github.com/login/oauth/authorize?")
    qs = parse_qs(urlparse(location).query)
    assert qs["client_id"] == ["Iv1.testclientid"]
    assert qs["state"][0]
    # redirect_uri는 절대 싣지 않는다(앱 등록 Callback URL에 의존하는 의도적 설계).
    assert "redirect_uri" not in qs


def test_login_sets_signed_state_cookie_with_httponly_and_samesite(client):
    r = client.get("/auth/github/login", follow_redirects=False)
    cookie_headers = r.headers.get_list("set-cookie")
    assert len(cookie_headers) == 1
    cookie_header = cookie_headers[0]
    assert "HttpOnly" in cookie_header
    assert "samesite=lax" in cookie_header.lower()
    assert "secure" in cookie_header.lower()


# ---------------------------------------------------------------------------
# /auth/github/install
# ---------------------------------------------------------------------------
def test_install_redirects_to_app_install_page_with_state(client):
    r = client.get("/auth/github/install", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    # 외부(GitHub) 계약값 — 설치 엔드포인트 경로는 리터럴로 못 박는다.
    assert location.startswith("https://github.com/apps/namu-memory-app/installations/new?")
    qs = parse_qs(urlparse(location).query)
    assert qs["state"][0]


def test_install_sets_state_cookie_like_login(client):
    """설치 왕복도 로그인과 같은 도장을 찍어야 한다 — 쿠키 속성이 한쪽만
    달라지면 왕복이 조용히 깨진다."""
    r = client.get("/auth/github/install", follow_redirects=False)
    cookie_headers = r.headers.get_list("set-cookie")
    assert len(cookie_headers) == 1
    cookie_header = cookie_headers[0]
    assert cookie_header.startswith(wa._STATE_COOKIE_NAME + "=")
    assert "HttpOnly" in cookie_header
    assert "samesite=lax" in cookie_header.lower()
    assert "secure" in cookie_header.lower()


def test_install_roundtrip_passes_callback_state_check(client, monkeypatch):
    """설치 왕복 회귀 테스트.

    2026-07-26 실사용에서 안내 화면이 GitHub 설치 주소를 직접 링크한 탓에
    설치 후 콜백이 state 없이 돌아와 400 Bad Request로 거절됐다(로그 실측:
    `callback?code=...&installation_id=149156594&setup_action=install`).
    install을 경유하면 같은 왕복이 통과해 저장소 목록 조회까지 가야 한다.
    """
    r = client.get("/auth/github/install", follow_redirects=False)
    state = _extract_state(r.headers["location"])
    fake, calls = _make_fake_http_json(repos=["octocat/namu-memory"])
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get(
        "/auth/github/callback",
        params={
            "state": state,
            "code": "goodcode",
            "installation_id": "149156594",
            "setup_action": "install",
        },
    )

    assert r.status_code == 200
    # state 검증을 통과해 설치 분기까지 들어갔다는 증거 — 저장소 목록을 실제로
    # 조회했다(400에서 멈췄다면 이 호출 자체가 없다).
    assert any(
        c["url"].startswith("https://api.github.com/user/installations/149156594/")
        for c in calls
    )


# ---------------------------------------------------------------------------
# /auth/github/callback — state 검증
# ---------------------------------------------------------------------------
def test_callback_without_state_cookie_rejected(client):
    r = client.get("/auth/github/callback", params={"state": "whatever", "code": "abc"})
    assert r.status_code == 400


def test_callback_state_mismatch_rejected(client):
    _do_login(client)  # state 쿠키는 심되, 콜백에는 다른 값을 보낸다
    r = client.get(
        "/auth/github/callback", params={"state": "attacker-supplied-state", "code": "abc"}
    )
    assert r.status_code == 400


def test_callback_missing_query_state_rejected(client):
    _do_login(client)
    r = client.get("/auth/github/callback", params={"code": "abc"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 토큰 교환 — HTTP 200이지만 본문에 error가 실린 경우
# ---------------------------------------------------------------------------
def test_callback_token_exchange_error_in_200_body_rejected(client, monkeypatch):
    state = _do_login(client)
    fake, calls = _make_fake_http_json(token_error="bad_verification_code")
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get("/auth/github/callback", params={"state": state, "code": "bad-code"})

    assert r.status_code == 400
    # status는 200이었지만 본문의 error 키를 봐서 실패 처리했는지 — 호출 자체는
    # 됐어야 한다(그래야 "본문을 실제로 봤다"가 증명된다).
    assert calls  # 토큰 엔드포인트가 호출되긴 했다
    # 사용자 정보 조회(다음 단계)까지는 진행하지 않았어야 한다.
    assert not any(c["url"] == "https://api.github.com/user" for c in calls)


def test_callback_token_exchange_error_with_access_token_present_still_rejected(client, monkeypatch):
    """error와 access_token이 응답에 동시에 실려도(실사용 GitHub에서는 안
    일어나지만) error를 봐서 거부해야 한다 — access_token이 있으면 그 뒤의
    'access_token 없음' 검사가 대신 400을 만들어 error 검사가 실제로 봤는지를
    가려버리는 함정이 있었다(2차 검수 지적 ③, access_token 부재 검사에 얹혀가는
    거짓 통과)."""
    state = _do_login(client)
    fake, calls = _make_fake_http_json(
        token_error="bad_verification_code", token_error_include_access_token=True
    )
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get("/auth/github/callback", params={"state": state, "code": "bad-code"})

    assert r.status_code == 400
    assert "should-not-be-trusted" not in r.text  # 신뢰하면 안 될 토큰이 화면에 안 새야 함
    assert not any(c["url"] == "https://api.github.com/user" for c in calls)


# ---------------------------------------------------------------------------
# 아웃바운드 요청의 외부(GitHub) 계약값 — 바디 키 이름/헤더/쿼리 파라미터는
# 우리가 고른 값이 아니라 GitHub이 정한 값이므로 리터럴로 못 박는다(2차 검수
# 지적 ④). calls를 기록만 하고 값을 검증하지 않으면, 키 이름이 실수로 바뀌어도
# (예: client_id → clientId) 어떤 테스트도 잡지 못한다.
# ---------------------------------------------------------------------------
def test_outbound_requests_use_github_contract_body_keys_headers_and_query(client, monkeypatch):
    state = _do_login(client)
    fake, calls = _make_fake_http_json(repos=["hank/namu-memory"])
    monkeypatch.setattr(wa, "_http_json", fake)

    client.get(
        "/auth/github/callback",
        params={"state": state, "code": "the-code-value", "installation_id": "1"},
    )

    token_calls = [c for c in calls if c["url"] == "https://github.com/login/oauth/access_token"]
    assert len(token_calls) == 1
    # GitHub 공식 문서(authorizing-oauth-apps.md)가 정한 바디 키 3개 — 리터럴.
    assert token_calls[0]["json_body"] == {
        "client_id": "Iv1.testclientid",
        "client_secret": "test-client-secret-marker-9f2a",
        "code": "the-code-value",
    }
    assert token_calls[0]["headers"] == {"Accept": "application/json"}

    user_calls = [c for c in calls if c["url"] == "https://api.github.com/user"]
    assert len(user_calls) == 1
    assert user_calls[0]["headers"] == {
        "Authorization": "Bearer user-token-abcdef",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    install_calls = [c for c in calls if "user/installations" in c["url"]]
    assert install_calls
    assert install_calls[0]["headers"] == {
        "Authorization": "Bearer user-token-abcdef",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # installation_id가 경로에 실제로 삽입됐는지, per_page/page 쿼리가 GitHub
    # OpenAPI 스펙 기본값 형태(정수 문자열)로 실렸는지까지 리터럴로 확인한다.
    assert install_calls[0]["url"].startswith(
        "https://api.github.com/user/installations/1/repositories?"
    )
    install_qs = parse_qs(urlparse(install_calls[0]["url"]).query)
    assert install_qs["per_page"] == ["100"]
    assert install_qs["page"] == ["1"]


# ---------------------------------------------------------------------------
# 로그인 복귀(installation_id 없음) — 안내 화면
# ---------------------------------------------------------------------------
def test_callback_login_only_return_sends_you_to_the_repository_step(client, monkeypatch):
    """저장소가 하나도 없는 사람은 2단계(저장소 마련하기) 화면으로 간다.

    그 화면을 콜백이 직접 그려 주지 않고 **주소로 넘기는** 이유: 새 탭에서
    저장소를 만들고 돌아올 자리라 자기 주소가 있어야 하고, 새로고침해도
    살아 있어야 한다(namu-70).
    """
    state = _do_login(client)
    fake, calls = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode"},
        follow_redirects=False,
    )

    assert r.status_code == 302
    assert r.headers["location"] == "/auth/repo"
    # installation_id가 없었으니 저장소 목록 조회는 아예 없었어야 한다.
    assert not any(c["url"].startswith("https://api.github.com/user/installations/") for c in calls)


def test_repo_step_puts_creating_the_repository_before_granting_access(client, monkeypatch):
    """순서가 이 화면의 존재 이유다.

    예전 화면은 [앱 설치]가 먼저 눈에 띄고 "저장소가 없으면 만드세요"가 아래
    딸린 문장이었다. 그런데 앱 설치 화면이 곧 저장소를 고르는 화면이라,
    저장소가 없는 사람은 고를 것이 없는 화면에 도착해 길이 끊겼다.
    """
    state = _do_login(client)
    fake, _calls = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)
    client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})

    body = client.get("/auth/repo").text

    create_at = body.find("https://github.com/new?")
    install_at = body.find('href="/auth/github/install"')
    assert 0 < create_at < install_at, "저장소 만들기가 권한 주기보다 먼저 나와야 한다"
    # 미리 채운 저장소 생성 링크 — 계약값 리터럴로 확인
    assert "name=namu-memory" in body
    assert "visibility=private" in body
    # 설치는 반드시 우리 경로를 경유한다(링크는 state 쿠키를 심지 못한다).
    assert "https://github.com/apps/" not in body
    # 만들고 돌아온 사람이 이어갈 길 — 이것이 이번에 새로 생긴 하나다.
    assert 'href="/auth/repo/done"' in body


def test_repo_step_needs_a_session(client):
    """남의 진행 화면을 세션 없이 열 수 있으면 안 된다."""
    assert client.get("/auth/repo").status_code == 401
    assert client.get("/auth/repo/done", follow_redirects=False).status_code == 401


def test_repo_step_does_not_dead_end_an_already_connected_member(client, monkeypatch):
    """연결을 끝낸 사람에게 이 화면은 할 일이 없는 막다른 길이다."""
    _connect_via_login(client, monkeypatch, github_id=40001, repo="pat/memories")

    r = client.get("/auth/repo", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/auth/me"


def test_i_made_it_button_carries_the_name_to_the_permission_step(client, monkeypatch):
    state = _do_login(client)
    fake, _calls = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)
    client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})

    r = client.get("/auth/repo/done", follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/auth/github/install"
    hint = [h for h in r.headers.get_list("set-cookie") if "namu_repo_hint" in h]
    assert hint and "HttpOnly" in hint[0]


def test_the_name_you_brought_skips_the_choosing_screen(client, monkeypatch):
    """방금 만들어 온 사람에게 "어느 거였죠?"라고 되묻지 않는다."""
    state = _do_login(client)
    fake, _calls = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)
    client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})
    client.get("/auth/repo/done", follow_redirects=False)  # 이름을 담는다

    state2 = _do_login(client)
    fake2, _c2 = _make_fake_http_json(
        repos=["octocat/old-notes", "octocat/namu-memory", "octocat/blog"]
    )
    monkeypatch.setattr(wa, "_http_json", fake2)
    r = client.get(
        "/auth/github/callback",
        params={"state": state2, "code": "goodcode", "installation_id": "77"},
    )

    assert "octocat/namu-memory" in r.text
    assert "저장소를 하나만 고르세요" not in r.text
    conn = identity.connect()
    try:
        user_key = wa._unsign_with_expiry(client.cookies.get("namu_session"))
        row = identity.get_by_user_key(conn, user_key)
    finally:
        conn.close()
    assert row["repo_full_name"] == "octocat/namu-memory"


def test_a_different_name_does_not_block_you(client, monkeypatch):
    """새 탭에서 이름을 바꿔 만든 사람도 막히지 않는다 — 담아 둔 이름이 목록에
    없으면 아무 일도 일어나지 않고 평소대로 고르기 화면이 나온다."""
    state = _do_login(client)
    fake, _calls = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)
    client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})
    client.get("/auth/repo/done", follow_redirects=False)

    state2 = _do_login(client)
    fake2, _c2 = _make_fake_http_json(repos=["octocat/my-brain", "octocat/blog"])
    monkeypatch.setattr(wa, "_http_json", fake2)
    r = client.get(
        "/auth/github/callback",
        params={"state": state2, "code": "goodcode", "installation_id": "77"},
    )

    assert "저장소를 하나만 고르세요" in r.text
    assert "octocat/my-brain" in r.text


def test_the_name_is_used_once_and_thrown_away(client, monkeypatch):
    """남겨 두면 나중에 저장소를 바꾸려고 다시 온 사람이 옛 이름으로 조용히
    연결된다."""
    state = _do_login(client)
    fake, _calls = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)
    client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})
    client.get("/auth/repo/done", follow_redirects=False)

    state2 = _do_login(client)
    fake2, _c2 = _make_fake_http_json(repos=["octocat/namu-memory", "octocat/blog"])
    monkeypatch.setattr(wa, "_http_json", fake2)
    r = client.get(
        "/auth/github/callback",
        params={"state": state2, "code": "goodcode", "installation_id": "77"},
        follow_redirects=False,
    )

    cleared = [
        h
        for h in r.headers.get_list("set-cookie")
        if "namu_repo_hint" in h and "Max-Age=0" in h
    ]
    assert cleared, "쓰고 난 이름표가 지워지지 않았다"


def test_callback_without_installation_id_finds_existing_installation(client, monkeypatch):
    """이미 설치한 사용자 회귀 테스트.

    2026-07-26 실사용에서, 앱을 이미 설치한 계정은 설치 링크가 설정 화면으로
    넘어가고 바꿀 것이 없어 Save가 비활성이라 GitHub이 installation_id를 실은
    왕복을 만들어 주지 않았다 — 저장소 연결을 영영 끝낼 수 없었다. 이제
    콜백이 사용자 토큰으로 기존 설치를 직접 조회해 이어가야 한다.
    """
    state = _do_login(client)
    fake, calls = _make_fake_http_json(
        installations=[149156594], repos=["octocat/namu-memory"]
    )
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})

    assert r.status_code == 200
    # 설치 하나 + 저장소 하나 = 고를 것이 없으므로 곧장 연결까지 간다.
    assert "연결 완료" in r.text
    assert "octocat/namu-memory" in r.text
    assert any(c["url"].startswith("https://api.github.com/user/installations?") for c in calls)


def test_callback_without_installation_id_multiple_repos_shows_picker(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(
        installations=[149156594], repos=["octocat/namu-memory", "octocat/other"]
    )
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})

    assert r.status_code == 200
    assert "저장소 선택" in r.text
    assert "octocat/namu-memory" in r.text
    assert "octocat/other" in r.text


def test_callback_without_installation_id_multiple_installations_merged(client, monkeypatch):
    """설치가 여러 개(개인 + 조직)면 한 화면에 모으되, 링크마다 그 저장소가 속한
    설치 번호를 실어야 select-repo 서명 검증을 통과한다."""
    state = _do_login(client)
    fake, _ = _make_fake_http_json(
        installations=[111, 222],
        repos_by_installation={111: ["octocat/personal"], 222: ["acme/team"]},
    )
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})

    assert r.status_code == 200
    body = r.text
    assert "octocat/personal" in body
    assert "acme/team" in body
    # 각 저장소 링크가 자기 설치 번호를 달고 있는지 — 뒤바뀌면 서명이 어긋난다.
    personal_link = re.search(r'href="([^"]*)"[^>]*>octocat/personal<', body).group(1)
    team_link = re.search(r'href="([^"]*)"[^>]*>acme/team<', body).group(1)
    assert parse_qs(urlparse(personal_link).query)["installation_id"] == ["111"]
    assert parse_qs(urlparse(team_link).query)["installation_id"] == ["222"]


def test_callback_merged_picker_links_pass_select_repo_signature(client, monkeypatch):
    """모아 보여준 링크를 그대로 눌렀을 때 실제로 연결까지 되는지 — 서명 계산이
    설치 번호와 짝이 맞는지를 왕복으로 확인한다."""
    state = _do_login(client)
    fake, _ = _make_fake_http_json(
        installations=[111, 222],
        repos_by_installation={111: ["octocat/personal"], 222: ["acme/team"]},
    )
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})
    link = re.search(r'href="([^"]*)"[^>]*>acme/team<', r.text).group(1)

    r2 = client.get(link, follow_redirects=False)

    assert r2.status_code == 200
    assert "연결 완료" in r2.text


def test_callback_session_cookie_set_after_login(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)

    # 저장소가 없는 사람은 2단계 화면으로 **넘겨진다** — 세션 쿠키는 그 넘기는
    # 응답 자체에 실려야 한다(따라간 뒤의 화면에는 실리지 않는다).
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    cookie_names = [h.split("=", 1)[0] for h in r.headers.get_list("set-cookie")]
    assert any(name != "" for name in cookie_names)
    # 세션 쿠키 자체가 HttpOnly인지도 함께 확인.
    session_set_cookie = next(
        h for h in r.headers.get_list("set-cookie") if "Max-Age=0" not in h
    )
    assert "HttpOnly" in session_set_cookie


# ---------------------------------------------------------------------------
# 설치 복귀 — 저장소 1개 → set_installation 실제 호출 확인
# ---------------------------------------------------------------------------
def test_callback_install_return_single_repo_calls_set_installation(client, monkeypatch, tmp_path):
    state = _do_login(client)
    fake, calls = _make_fake_http_json(github_id=2002, repos=["alice/namu-memory"])
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "555"},
    )

    assert r.status_code == 200
    assert "연결 완료" in r.text
    assert "alice/namu-memory" in r.text

    # 실제 신원 장부에 반영됐는지(핵심 요구사항 — set_installation이 진짜 그
    # 값으로 불렸는지) DB를 직접 열어 확인한다.
    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 2002)
    finally:
        conn.close()
    assert row is not None
    assert row["installation_id"] == 555
    assert row["repo_full_name"] == "alice/namu-memory"

    assert any(
        c["url"].startswith("https://api.github.com/user/installations/555/repositories")
        for c in calls
    )


# ---------------------------------------------------------------------------
# 설치 복귀 — 저장소 2개 이상 → 선택 화면, 아직 set_installation 안 됨
# ---------------------------------------------------------------------------
def test_callback_install_return_multiple_repos_shows_selection_not_set_yet(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(
        github_id=3003, repos=["bob/repo-a", "bob/repo-b"]
    )
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "777"},
    )

    assert r.status_code == 200
    assert "repo-a" in r.text
    assert "repo-b" in r.text
    assert "select-repo" in r.text

    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 3003)
    finally:
        conn.close()
    assert row is not None
    assert row["installation_id"] is None  # 아직 고르지 않았다
    assert row["repo_full_name"] is None


# ---------------------------------------------------------------------------
# _fetch_installation_repos — 페이지네이션 전체 순회 + 안전 상한 (2차 검수
# 지적 ② — 첫 페이지(기본 30개)만 받고 조용히 끝나던 실제 코드 결함)
# ---------------------------------------------------------------------------
def test_fetch_installation_repos_paginates_across_multiple_pages(monkeypatch):
    # per_page=100이므로 130개면 반드시 2페이지에 걸쳐야 전부 모인다 — 예전
    # 코드처럼 첫 페이지만 받으면 100개에서 멈춘다.
    all_repos = [f"org/repo-{i}" for i in range(130)]
    fake, calls = _make_fake_http_json(repos=all_repos)
    monkeypatch.setattr(wa, "_http_json", fake)

    names, truncated = wa._fetch_installation_repos("user-token", 42)

    assert names == all_repos
    assert truncated is False
    install_calls = [c for c in calls if "user/installations" in c["url"]]
    pages_requested = sorted(
        int(parse_qs(urlparse(c["url"]).query)["page"][0]) for c in install_calls
    )
    assert pages_requested == [1, 2]  # 두 페이지 다 실제로 요청했는지


def test_fetch_installation_repos_hits_safety_cap_and_flags_truncated(monkeypatch):
    # 안전 상한을 2페이지로 낮춰 빠르게 재현한다. total_count도 상한을 훨씬
    # 넘는 값으로 줘서 "끝까지 못 갔다"를 보장한다.
    monkeypatch.setattr(wa, "_INSTALLATION_REPOS_MAX_PAGES", 2)
    all_repos = [f"org/repo-{i}" for i in range(500)]
    fake, calls = _make_fake_http_json(repos=all_repos, repos_total_count=500)
    monkeypatch.setattr(wa, "_http_json", fake)

    names, truncated = wa._fetch_installation_repos("user-token", 42)

    assert truncated is True
    assert len(names) == 200  # 상한(2페이지) x per_page(100)에서 정확히 멈췄다
    install_calls = [c for c in calls if "user/installations" in c["url"]]
    assert len(install_calls) == 2  # 무한 루프 없이 상한에서 멈췄다(호출 횟수 유한)


def test_fetch_installation_repos_stops_on_empty_page_without_total_count(monkeypatch):
    """응답에 total_count가 없으면 "빈 페이지가 오면 멈춘다"가 유일한 정지
    조건이 된다 — 그 1차 방어선을 직접 고정하는 계약 테스트(2차 재검수 권고).

    GitHub 공식 OpenAPI 스펙상 total_count는 required라 실사용에서 이 경로는
    사실상 발생하지 않지만, 그렇기 때문에 오히려 방어선이 조용히 사라져도
    아무도 모른다. 상한(50페이지)이 최종 방어선으로 남아 무한 루프 자체는
    불가능하므로 이건 취약점이 아니라 "방어가 무력화된 상태"를 잡는 테스트다.
    """
    all_repos = [f"org/repo-{i}" for i in range(150)]

    def _fake(method, url, *, headers=None, json_body=None):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        per_page = int(parse_qs(urlparse(url).query)["per_page"][0])
        chunk = all_repos[(page - 1) * per_page : page * per_page]
        # total_count를 의도적으로 **빼고** 돌려준다.
        return 200, {"repositories": [{"full_name": n} for n in chunk]}

    calls = []

    def _counting(method, url, **kwargs):
        calls.append(url)
        return _fake(method, url, **kwargs)

    monkeypatch.setattr(wa, "_http_json", _counting)

    names, truncated = wa._fetch_installation_repos("user-token", 42)

    assert names == all_repos
    assert truncated is False
    # 1(100개) → 2(50개) → 3(빈 페이지에서 정지). 빈 페이지 break가 사라지면
    # 상한(50페이지)까지 계속 긁으므로 이 숫자가 어긋난다.
    assert len(calls) == 3


def test_callback_install_return_truncated_shows_warning_and_skips_auto_connect(client, monkeypatch):
    """상한에 걸려 잘렸으면(설령 그 결과 이 요청에서 딱 1개만 잡혔더라도) 화면에
    반드시 알리고, 저장소 1개 자동 연결 경로를 타지 않는다 — 조용한 누락이
    결함의 본질이므로 조용히 자르는 대안으로 바꾸지 않는다."""
    monkeypatch.setattr(wa, "_INSTALLATION_REPOS_MAX_PAGES", 1)
    state = _do_login(client)
    all_repos = [f"grace/repo-{i}" for i in range(150)]
    fake, _ = _make_fake_http_json(github_id=9009, repos=all_repos, repos_total_count=150)
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "444"},
    )

    assert r.status_code == 200
    assert "truncated" in r.text.lower()  # 경고 문구가 실제로 화면에 나왔는지

    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 9009)
    finally:
        conn.close()
    assert row["installation_id"] is None  # 잘렸으면 자동 연결하지 않는다


def test_callback_install_return_zero_repos_shows_guidance(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=4004, repos=[])
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "888"},
    )
    assert r.status_code == 200
    assert "저장소" in r.text

    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 4004)
    finally:
        conn.close()
    assert row["installation_id"] is None


# ---------------------------------------------------------------------------
# select-repo — 서명 위조/부재/세션 부재
# ---------------------------------------------------------------------------
def _select_repo_link_from_html(body: str) -> str:
    match = re.search(r'href="(/auth/github/select-repo\?[^"]+)"', body)
    assert match, "선택 화면 HTML에서 select-repo 링크를 찾지 못함"
    return match.group(1)


def test_select_repo_valid_link_from_callback_succeeds(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=5005, repos=["carol/repo-x", "carol/repo-y"])
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "999"},
    )
    link = _select_repo_link_from_html(r.text)

    r2 = client.get(link)
    assert r2.status_code == 200
    assert "연결 완료" in r2.text

    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 5005)
    finally:
        conn.close()
    assert row["installation_id"] == 999
    assert row["repo_full_name"] in ("carol/repo-x", "carol/repo-y")


def test_select_repo_without_sig_rejected(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=6006, repos=["dave/repo-a", "dave/repo-b"])
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "111"},
    )
    link = _select_repo_link_from_html(r.text)
    link_no_sig = re.sub(r"&sig=[^&]+", "", link)
    assert "sig=" not in link_no_sig

    r2 = client.get(link_no_sig)
    assert r2.status_code == 403

    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 6006)
    finally:
        conn.close()
    assert row["installation_id"] is None


def test_select_repo_forged_repo_name_rejected(client, monkeypatch):
    """콜백이 실제 발급한 sig를 그대로 두고 repo만 남의(다른) 저장소로 바꿔치면
    서명이 어긋나 거부돼야 한다 — 손으로 URL을 고쳐 남의 repo를 밀어넣는 공격."""
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=7007, repos=["erin/repo-a", "erin/repo-b"])
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "222"},
    )
    link = _select_repo_link_from_html(r.text)
    forged = re.sub(r"repo=erin%2Frepo-[ab]", "repo=someone-else%2Fprivate-repo", link)
    assert forged != link

    r2 = client.get(forged)
    assert r2.status_code == 403

    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 7007)
    finally:
        conn.close()
    assert row["installation_id"] is None


def test_select_repo_link_from_other_user_session_rejected(monkeypatch):
    """A에게 발급된 select-repo 링크를 **다른 세션(B)** 이 통째로 재사용하면
    거부돼야 한다.

    위 test_select_repo_forged_repo_name_rejected는 "같은 세션에서 repo만
    바꿔치기"를 시험한다 — 겹치는 것처럼 보이지만 이 경로는 못 잡는다. 링크를
    그대로 쓰면 repo도 installation_id도 서명과 일치하므로, 막아주는 것은
    오직 서명 페이로드에 **user_key가 함께 들어 있다는 사실 하나**다
    (web_auth._repo_link_sig). 그 한 조각을 빼도 스위트가 전부 통과하던 사각을
    메우는 테스트다(2차 재검수 ⓓ) — payload에서 user_key를 제거하면 B 계정에
    A의 저장소가 실제로 연결되는 것이 재현됐다.
    """
    client_a = TestClient(wa.build_auth_app(), base_url="https://testserver")
    state_a = _do_login(client_a)
    fake_a, _ = _make_fake_http_json(
        github_id=1111, repos=["victim/repo-a", "victim/repo-b"]
    )
    monkeypatch.setattr(wa, "_http_json", fake_a)
    r = client_a.get(
        "/auth/github/callback",
        params={"state": state_a, "code": "goodcode", "installation_id": "321"},
    )
    link = _select_repo_link_from_html(r.text)

    # 공격자 B — 정상적으로 자기 계정으로 로그인해 유효한 세션 쿠키를 갖는다.
    client_b = TestClient(wa.build_auth_app(), base_url="https://testserver")
    state_b = _do_login(client_b)
    fake_b, _ = _make_fake_http_json(github_id=2222)
    monkeypatch.setattr(wa, "_http_json", fake_b)
    client_b.get("/auth/github/callback", params={"state": state_b, "code": "goodcode"})

    r2 = client_b.get(link)  # A용 링크를 손대지 않고 그대로 재사용
    assert r2.status_code == 403

    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 2222)
    finally:
        conn.close()
    assert row["installation_id"] is None  # B 계정에 A의 repo가 붙으면 안 된다


def test_select_repo_no_session_cookie_rejected():
    # 새 클라이언트(쿠키 없음)로 직접 호출 — 로그인/콜백을 전혀 거치지 않았다.
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    r = fresh_client.get(
        "/auth/github/select-repo",
        params={"installation_id": "1", "repo": "x/y", "sig": "deadbeef"},
    )
    assert r.status_code == 401


def test_select_repo_malformed_session_cookie_rejected():
    """세션 쿠키 값이 애초에 우리 서명 포맷(`만료epoch|payload` + `.` + HMAC hex)이
    아닌 경우 — `_unsign_with_expiry`의 형식 검사(`"|" not in raw`)에서 걸러지고
    HMAC 비교(`hmac.compare_digest`)까지는 도달조차 하지 않는다. HMAC 검증
    자체를 시험하는 건 아래
    test_select_repo_well_formed_but_forged_session_cookie_rejected다 — 이름과
    실제로 시험하는 대상이 어긋나 있던 이전 버전(형식 오류를 "위조 거부"로
    잘못 이름 붙였던 결함)을 이 이름으로 바로잡는다."""
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    # domain=을 명시하면(httpx 쿠키jar 실측 확인) 이 base_url 조합에서 쿠키가
    # 조용히 전송에서 빠진다 — "쿠키가 없다"와 "쿠키가 위조됐다"가 똑같이 401로
    # 보여서 구분이 안 되는 시험 자체 결함이 될 뻔했다(도메인 인자를 빼야 실제로
    # 전송된다). 아래에서 실제로 전송됐는지까지 확인해 재발을 막는다.
    fresh_client.cookies.set("namu_session", "gh-999.deadbeefdeadbeef")
    r = fresh_client.get(
        "/auth/github/select-repo",
        params={"installation_id": "1", "repo": "x/y", "sig": "deadbeef"},
    )
    assert "namu_session=gh-999.deadbeefdeadbeef" in r.request.headers.get("cookie", "")
    assert r.status_code == 401


def test_select_repo_well_formed_but_forged_session_cookie_rejected():
    """형식(`만료epoch|payload` + HMAC)은 유효하지만 서명(mac)만 틀린 세션
    쿠키를 **로그인 없이 직접 주입**한다 — 실제 로그인 흐름으로 만들면 서명이
    맞아버려서 이 경로(HMAC 비교 자체)를 절대 지나지 않는다.

    이건 `_unsign`의 `hmac.compare_digest(mac, _hmac_hex(value))` 검사가 실제로
    지켜지고 있는지를 시험하는 테스트다 — 그 줄을 통째로 지워도 통과했던 게
    2차 검수에서 실측된 결함이다(비밀키를 몰라도 형식만 맞추면 임의 github_id로
    세션을 위조할 수 있게 되는 회귀)."""
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    expires_at = int(time.time()) + 1800
    forged = f"{expires_at}|gh-1.{'0' * 64}"  # 형식은 유효, mac은 확실히 틀림
    fresh_client.cookies.set("namu_session", forged)  # domain= 없이(위 함수 주석 참고)
    r = fresh_client.get(
        "/auth/github/select-repo",
        params={"installation_id": "1", "repo": "x/y", "sig": "irrelevant"},
    )
    # 쿠키가 실제로 전송됐는지부터 확인 — 안 됐다면 아래 401 단언이 "세션 없음"과
    # "서명 위조"를 구분 못 하는 거짓 통과가 된다(위 함수에서 실측된 함정).
    assert f"namu_session={forged}" in r.request.headers.get("cookie", "")
    assert r.status_code == 401


def test_callback_well_formed_but_forged_state_cookie_rejected(client, monkeypatch):
    """state 쿠키도 동일한 성질의 위조를 시험한다 — /login을 거치지 않고 형식만
    맞춘(만료 형식 O, HMAC은 틀림) 값을 직접 주입한다. `_unsign`은 login의 state
    쿠키, callback의 state/session 쿠키, select_repo의 session 쿠키 검증에 전부
    쓰이는 공통 신뢰 앵커라 이 케이스도 별도로 시험해야 한다.

    `_http_json`을 반드시 monkeypatch해 "호출 자체가 없었다"까지 확인한다 —
    실측 함정: 이 테스트 환경은 실제 인터넷에 나갈 수 있어서, state 검증이
    뚫려 다음 단계(토큰 교환)까지 흘러가도 실제 GitHub가 가짜 client_id에 404를
    돌려줘 우연히 400이 나오고, status_code만 보면 "state 검증이 막았다"와
    "다른 이유로 실패했다"를 구분하지 못한다(HMAC 비교를 지워보고서야 실측됨)."""
    fake, calls = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)

    expires_at = int(time.time()) + 600
    forged = f"{expires_at}|attacker-state.{'0' * 64}"
    client.cookies.set("namu_oauth_state", forged)  # domain= 없이(위에서 실측된 함정 참고)
    r = client.get("/auth/github/callback", params={"state": "attacker-state", "code": "c"})
    assert f"namu_oauth_state={forged}" in r.request.headers.get("cookie", "")
    assert r.status_code == 400
    assert calls == [], "state 검증을 통과해 토큰 교환까지 흘러갔다(실제로 막았어야 한다)"


# ---------------------------------------------------------------------------
# 비밀 유출 금지 — 응답 본문/로그
# ---------------------------------------------------------------------------
def test_secrets_not_leaked_in_response_body(client, monkeypatch, caplog):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=8008, repos=["frank/namu-memory"])
    monkeypatch.setattr(wa, "_http_json", fake)

    with caplog.at_level("DEBUG"):
        r = client.get(
            "/auth/github/callback",
            params={"state": state, "code": "goodcode", "installation_id": "333"},
        )

    assert "test-client-secret-marker-9f2a" not in r.text
    assert "user-token-abcdef" not in r.text
    assert "test-session-secret-value" not in r.text
    assert "test-client-secret-marker-9f2a" not in caplog.text
    assert "user-token-abcdef" not in caplog.text
    assert "test-session-secret-value" not in caplog.text


def test_secrets_not_leaked_on_token_exchange_error(client, monkeypatch, caplog):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(token_error="bad_verification_code")
    monkeypatch.setattr(wa, "_http_json", fake)

    with caplog.at_level("DEBUG"):
        r = client.get("/auth/github/callback", params={"state": state, "code": "bad"})

    assert "test-client-secret-marker-9f2a" not in r.text
    assert "test-client-secret-marker-9f2a" not in caplog.text


# ---------------------------------------------------------------------------
# routing_server.build_app() — /auth/ 우회, 그 외 전부 인증 (namu-cloud-routing
# 회귀 방지: 인증 우회 경로가 실수로 넓어지면 안 된다)
# ---------------------------------------------------------------------------
def test_build_app_auth_route_reachable_without_token(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_TOKEN", "supersecrettoken")
    app = rs.build_app()
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/auth/github/login", follow_redirects=False)
    assert r.status_code == 302  # 인증 없이도 도달 — 로그인 전이라 토큰이 없는 게 정상


def test_build_app_mcp_route_rejects_request_without_per_user_secret(monkeypatch, tmp_path):
    """열쇠 없는 맨 `/mcp`는 도구에 닿지 못한다.

    namu-59 이전에는 여기서 401(토큰 없음)이 나왔다. 지금은 토큰 검사에 닿기
    전에 사용자별 열쇠 검사가 먼저 끊으므로 404다 — 어느 쪽이든 '통과 아님'이고,
    "없는 열쇠와 형식 오류를 구분해 주지 않는다"는 설계와 응답이 일치한다.
    """
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_TOKEN", "supersecrettoken")
    app = rs.build_app()
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/mcp")
    assert r.status_code == 404


async def _raw_asgi_get(app, path: str) -> int:
    """httpx/TestClient는 URL을 만들 때 dot-segment(`..`)를 클라이언트 쪽에서
    미리 정규화해 버린다(실측 확인: '/mcp/../auth/x' 요청이 실제로는 이미
    '/auth/x'로 합쳐진 채 서버에 도달한다) — 그래서 "조작된 리터럴 경로 문자열이
    서버에 그대로 왔을 때" 디스패처가 안전한 쪽을 고르는지는 TestClient로는 검증이
    안 되고, ASGI scope를 직접 만들어 문자열 그대로를 서버(dispatcher)에 넣어봐야
    한다.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": ("test", 1234),
        "server": ("testserver", 443),
        "scheme": "https",
    }
    status_holder: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status_holder["status"] = message["status"]

    await app(scope, receive, send)
    return status_holder["status"]


def test_build_app_path_traversal_toward_mcp_stays_out_of_auth_app(monkeypatch, tmp_path):
    """리터럴 경로 문자열이 `/auth/`로 시작하지 않으면(정규화 여부와 무관하게)
    반드시 MCP+Auth 쪽으로 가야 한다 — '/mcp/../auth/...'는 문자 그대로
    `/auth/`로 시작하지 않으므로 auth_app을 절대 타면 안 된다.

    200이면 우회 성공 = 회귀. namu-59부터는 401이 아니라 404다(열쇠 조각이
    '..'이라 장부에서 찾을 수 없어 사용자별 열쇠 검사에서 먼저 끊긴다)."""
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_TOKEN", "supersecrettoken")
    app = rs.build_app()
    status = asyncio.run(_raw_asgi_get(app, "/mcp/../auth/github/login"))
    assert status == 404
    assert status != 200  # 우회 성공 여부가 이 테스트의 본질


def test_build_app_path_traversal_toward_auth_never_reaches_mcp(monkeypatch, tmp_path):
    """반대 방향: `/auth/`로 시작하는 조작 경로는 auth_app으로 가되(그 자체가
    안전한 방향), MCP 앱에 도달해서는 안 된다 — auth_app에 해당 라우트가 없어
    404가 나오는 것으로 "MCP에 안 갔다"를 확인한다(200 MCP 응답이 아니라는 것이
    핵심)."""
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_TOKEN", "supersecrettoken")
    app = rs.build_app()
    status = asyncio.run(_raw_asgi_get(app, "/auth/../mcp"))
    assert status == 404


# ---------------------------------------------------------------------------
# 연결 완료 화면이 완성된 MCP 접속 주소를 보여준다 (namu-59)
#
# 이 화면이 주소를 안 주면 사용자는 주소를 조립할 방법이 없다 — 예전에는
# "별도 대시보드에서 확인하세요"라고만 적혀 있었고 그 대시보드가 없었다.
# ---------------------------------------------------------------------------
def test_connected_page_shows_full_mcp_url(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=7007, repos=["erin/memories"])
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "777"},
    )
    assert r.status_code == 200
    assert "연결 완료" in r.text

    conn = identity.connect()
    try:
        secret = identity.get_by_github_id(conn, 7007)["mcp_secret"]
    finally:
        conn.close()

    # 화면에 그 사용자의 진짜 열쇠가 실린 완성 주소가 통째로 있어야 한다.
    assert f"/mcp/{secret}?client=claude" in r.text
    assert "testserver" in r.text          # 바깥 호스트가 붙어 있어야 복붙이 된다
    assert "대시보드에서 확인" not in r.text  # 옛 안내 문구가 남아 있으면 회귀


def test_connected_page_url_is_per_user_not_shared(client, monkeypatch):
    """두 사용자의 완료 화면에 서로 다른 주소가 떠야 한다 — 같은 값이 뜨면
    전원 공용 열쇠 시절로 되돌아간 것이다."""
    urls = []
    for gh_id in (8008, 9009):
        state = _do_login(client)
        fake, _ = _make_fake_http_json(github_id=gh_id, repos=[f"u{gh_id}/mem"])
        monkeypatch.setattr(wa, "_http_json", fake)
        r = client.get(
            "/auth/github/callback",
            params={"state": state, "code": "goodcode", "installation_id": str(gh_id)},
        )
        match = re.search(r"https?://[^\s\"<]+/mcp/[^\s\"<]+", r.text)
        assert match, "완료 화면에서 접속 주소를 찾지 못했다"
        urls.append(match.group(0))
    assert urls[0] != urls[1]


def test_connected_page_respects_forwarded_proto(client, monkeypatch):
    """Cloudflare 터널 뒤라 원래 scheme이 http로 보일 수 있다 — 프록시가 붙여
    주는 x-forwarded-proto를 따르지 않으면 http:// 주소를 내주게 되고,
    사용자가 그대로 붙이면 커넥터가 붙지 않는다."""
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=10010, repos=["frank/mem"])
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "1010"},
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "namu-cloud.onnamu.kr"},
    )
    assert "https://namu-cloud.onnamu.kr/mcp/" in r.text


# ---------------------------------------------------------------------------
# /auth/me — 내 페이지(namu-60). 연결 완료 화면이 그 순간에만 보여주던 접속
# 주소를, 창을 닫은 뒤에도 다시 볼 수 있어야 한다는 것이 이 기능의 존재
# 이유다.
# ---------------------------------------------------------------------------
def _connect_via_login(client, monkeypatch, *, github_id, repo) -> dict:
    """login → callback을 밟아 사용자를 저장소까지 연결하고 그 장부 행을
    돌려준다(user_key/mcp_secret/repo_full_name을 포함) — 클라이언트 세션에는
    로그인 쿠키가 남는다."""
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=github_id, repos=[repo])
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": str(github_id)},
    )
    assert r.status_code == 200
    assert "연결 완료" in r.text
    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, github_id)
    finally:
        conn.close()
    assert row is not None
    return row


def test_me_shows_mcp_url_repo_and_user_key(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=20001, repo="paul/memories")

    r = client.get("/auth/me")

    assert r.status_code == 200
    assert "paul/memories" in r.text
    assert row["user_key"] in r.text
    assert f"/mcp/{row['mcp_secret']}?client=claude" in r.text
    assert 'href="/auth/logout"' in r.text
    # 연결 완료 화면과 같은 경고 문구 재사용(중복 붙여넣기가 아니라 공통 조각).
    assert "남에게 알려주지" in r.text


def test_me_no_session_rejected_without_leaking_info(monkeypatch):
    # 다른 클라이언트로 실제 연결된 사용자를 하나 만들어, 그 사람의 진짜
    # 값(주소/저장소/키)을 알아둔다 — 세션 없는 요청의 응답에 그 값이 전혀
    # 없어야 "노출 0"이 증명된다.
    victim_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    row = _connect_via_login(
        victim_client, monkeypatch, github_id=20002, repo="victim/secret-repo"
    )

    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    r = fresh_client.get("/auth/me")

    assert r.status_code == 401
    assert row["user_key"] not in r.text
    assert row["mcp_secret"] not in r.text
    assert "victim/secret-repo" not in r.text
    assert 'href="/auth/github/login"' in r.text


def test_me_forged_session_signature_rejected_without_leaking_info(monkeypatch):
    """실제 로그인으로는 서명이 항상 맞으므로, 이 케이스는 반드시 로그인
    없이 형식만 맞춘(HMAC은 틀린) 값을 직접 주입해야 진짜 서명 검증 경로를
    태운다(모듈 안내에 있는 766~794줄 선례와 동일한 원칙)."""
    victim_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    row = _connect_via_login(
        victim_client, monkeypatch, github_id=20003, repo="victim/other-repo"
    )
    user_key = row["user_key"]

    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    expires_at = int(time.time()) + 1800
    forged = f"{expires_at}|{user_key}.{'0' * 64}"  # 형식(만료+payload)은 유효, mac은 틀림
    fresh_client.cookies.set(wa._SESSION_COOKIE_NAME, forged)

    r = fresh_client.get("/auth/me")

    assert f"{wa._SESSION_COOKIE_NAME}={forged}" in r.request.headers.get("cookie", "")
    assert r.status_code == 401
    assert user_key not in r.text
    assert row["mcp_secret"] not in r.text
    assert "victim/other-repo" not in r.text


def test_me_expired_session_rejected_without_leaking_info(monkeypatch):
    """서명은 진짜(_sign_with_expiry로 실제 발급)지만 만료 시각이 과거인
    세션 — 형식/서명 검증이 아니라 만료 검사 자체가 동작하는지를 본다."""
    victim_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    row = _connect_via_login(
        victim_client, monkeypatch, github_id=20004, repo="victim/third-repo"
    )
    user_key = row["user_key"]

    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    expired = wa._sign_with_expiry(user_key, -10)  # 10초 전에 이미 만료
    fresh_client.cookies.set(wa._SESSION_COOKIE_NAME, expired)

    r = fresh_client.get("/auth/me")

    assert f"{wa._SESSION_COOKIE_NAME}={expired}" in r.request.headers.get("cookie", "")
    assert r.status_code == 401
    assert user_key not in r.text
    assert row["mcp_secret"] not in r.text
    assert "victim/third-repo" not in r.text


def test_me_valid_session_but_missing_from_ledger_treated_as_login_required(monkeypatch, tmp_path):
    """서명은 유효한 세션이지만(우리가 실제로 발급) 장부에서 그 사용자가
    사라진 경우(장부 재구축 등) — 500으로 터지지 않고 로그인 안내와 동일하게
    처리해야 한다."""
    client_a = TestClient(wa.build_auth_app(), base_url="https://testserver")
    row = _connect_via_login(client_a, monkeypatch, github_id=20005, repo="ghost/repo")

    # 장부에서 그 사용자를 직접 지운다(개발자 본인 계정으로는 재현할 수 없는
    # 상태를 의도적으로 만든다).
    conn = sqlite3.connect(str(tmp_path / "identity.db"))
    try:
        conn.execute("DELETE FROM users WHERE user_key = ?", (row["user_key"],))
        conn.commit()
    finally:
        conn.close()

    r = client_a.get("/auth/me")

    assert r.status_code == 401
    assert "로그인이 필요합니다" in r.text
    assert "/auth/github/login" in r.text
    assert row["mcp_secret"] not in r.text


def test_me_without_connected_repo_shows_install_guidance(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=20006, repos=[])  # 저장소 0개 → 미연결 상태
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "20006"},
    )
    assert r.status_code == 200

    conn = identity.connect()
    try:
        row = identity.get_by_github_id(conn, 20006)
    finally:
        conn.close()
    assert row["installation_id"] is None  # 미연결 상태를 실제로 만들었는지 확인

    r2 = client.get("/auth/me")

    assert r2.status_code == 200
    assert row["user_key"] in r2.text  # 사용자 키는 보여도 된다
    assert 'href="/auth/github/install"' in r2.text
    assert "/mcp/" not in r2.text  # 접속 주소는 아직 없다


def test_me_backfills_missing_mcp_secret_for_legacy_account(client, monkeypatch, tmp_path):
    """옛 계정(가입은 됐지만 mcp_secret 칸이 비어 있음)은 개발자 본인 계정으로는
    재현되지 않는 상태다 — DB를 직접 건드려 그 상태를 의도적으로 만든다."""
    row = _connect_via_login(client, monkeypatch, github_id=20007, repo="helen/legacy-memories")

    db_path = tmp_path / "identity.db"
    raw_conn = sqlite3.connect(str(db_path))
    try:
        raw_conn.execute(
            "UPDATE users SET mcp_secret = NULL WHERE user_key = ?", (row["user_key"],)
        )
        raw_conn.commit()
        cleared = raw_conn.execute(
            "SELECT mcp_secret FROM users WHERE user_key = ?", (row["user_key"],)
        ).fetchone()
        assert cleared[0] is None  # 옛 계정 상태가 실제로 만들어졌는지
    finally:
        raw_conn.close()

    r = client.get("/auth/me")

    assert r.status_code == 200
    assert "helen/legacy-memories" in r.text
    assert "/mcp/" in r.text  # 빈 화면이 아니라 주소가 실제로 채워졌는지

    conn = identity.connect()
    try:
        healed = identity.get_by_user_key(conn, row["user_key"])
    finally:
        conn.close()
    assert healed["mcp_secret"]  # 새 열쇠가 발급돼 저장됐는지
    assert f"/mcp/{healed['mcp_secret']}?client=claude" in r.text


# ---------------------------------------------------------------------------
# /auth/logout
# ---------------------------------------------------------------------------
def test_logout_clears_session_cookie_and_shows_login_link(client, monkeypatch):
    _connect_via_login(client, monkeypatch, github_id=20008, repo="ivan/memories")
    assert client.get("/auth/me").status_code == 200  # 로그아웃 전에는 보인다

    r = client.get("/auth/logout")
    assert r.status_code == 200
    assert "로그아웃" in r.text
    assert 'href="/auth/github/login"' in r.text

    r2 = client.get("/auth/me")
    assert r2.status_code == 401  # 쿠키가 실제로 지워져 더는 인증되지 않는다


# ---------------------------------------------------------------------------
# 로그인 왕복 마무리 — 이미 연결된 사용자는 저장소를 다시 고르라고 하지 않고
# 내 페이지로 이어진다.
# ---------------------------------------------------------------------------
def test_callback_already_connected_redirects_to_me_without_reselecting_repo(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=20009, repo="gina/memories")

    # 두 번째 로그인 왕복 — installation_id 없이 돌아온 순수 재로그인.
    state2 = _do_login(client)
    fake2, calls2 = _make_fake_http_json(github_id=20009)
    monkeypatch.setattr(wa, "_http_json", fake2)

    r2 = client.get(
        "/auth/github/callback",
        params={"state": state2, "code": "goodcode-2"},
        follow_redirects=False,
    )

    assert r2.status_code == 302
    assert r2.headers["location"] == "/auth/me"
    # 세션 쿠키가 이 리다이렉트 응답 자체에 실렸는지(내 페이지가 쿠키 없이는
    # 아무것도 못 보여주므로).
    assert any(
        h.startswith(wa._SESSION_COOKIE_NAME + "=") for h in r2.headers.get_list("set-cookie")
    )
    # 이미 연결돼 있으니 설치/저장소 목록을 다시 조회하지 않았어야 한다(재선택
    # 회피의 직접 증거).
    assert not any("installations" in c["url"] for c in calls2)

    r3 = client.get("/auth/me")
    assert r3.status_code == 200
    assert "gina/memories" in r3.text
    assert row["user_key"] in r3.text


# ---------------------------------------------------------------------------
# 온보딩 안내(namu-60) — 주소만 던져주면 초보자는 "이걸로 뭘 하라는 건지" 모른다.
#
# 핵심 요구: 연결 완료 화면과 내 페이지에 **같은 안내**가 나와야 한다(완료
# 화면에서만 설명하면 창을 닫은 사람은 다시 볼 방법이 없다).
# ---------------------------------------------------------------------------
_CLAUDE_CONNECTOR_STEPS = ["설정", "커넥터", "사용자 정의 커넥터", "붙여"]
# 저장소의 마크다운이 아니라 펴낸 안내서 사이트를 가리켜야 한다 — 마크다운 쪽은
# namu-74에서 "이 문서는 옮겨졌습니다" 표지판만 남아, 그리로 보내면 방문자가 한 번
# 더 눌러야 진짜 안내서에 닿는다.
_SELF_HOST_GUIDE_URL = (
    "https://onmiso-hash.github.io/namu-agent/docs/remote_mcp_guide.html"
)
_PLUGIN_GUIDE_URL = "https://onmiso-hash.github.io/namu-agent/docs/install_guide.html"


def _assert_onboarding_guide(body: str) -> None:
    for step in _CLAUDE_CONNECTOR_STEPS:
        assert step in body, f"웹 AI 연결 절차 안내에 '{step}'가 없다"
    assert _SELF_HOST_GUIDE_URL in body, "셀프호스팅 안내서 링크가 없다"
    assert _PLUGIN_GUIDE_URL in body, "플러그인 설치 안내서 링크가 없다"
    assert "플러그인" in body, "Claude Code·agy는 플러그인 설치라는 안내가 없다"


def test_connected_page_shows_onboarding_guide(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json(github_id=30001, repos=["kate/memories"])
    monkeypatch.setattr(wa, "_http_json", fake)
    r = client.get(
        "/auth/github/callback",
        params={"state": state, "code": "goodcode", "installation_id": "30001"},
    )
    assert r.status_code == 200
    _assert_onboarding_guide(r.text)


def test_me_page_shows_the_same_onboarding_guide(client, monkeypatch):
    _connect_via_login(client, monkeypatch, github_id=30002, repo="leo/memories")

    r = client.get("/auth/me")

    assert r.status_code == 200
    _assert_onboarding_guide(r.text)


def test_onboarding_guide_folds_only_the_side_paths(client, monkeypatch):
    """웹 AI 붙이는 법(2번)은 펼쳐진 상태여야 하고, 곁가지(셀프호스팅·플러그인)만
    접혀 있어야 한다 — 이 화면에 온 사람이 지금 해야 할 일이 2번이다."""
    _connect_via_login(client, monkeypatch, github_id=30003, repo="mia/memories")
    body = client.get("/auth/me").text

    # 곁가지 두 링크는 <details> 안에 있다.
    details_blocks = re.findall(r"<details>.*?</details>", body, flags=re.S)
    assert len(details_blocks) == 2
    folded = "".join(details_blocks)
    assert _SELF_HOST_GUIDE_URL in folded
    assert _PLUGIN_GUIDE_URL in folded
    # claude.ai 절차는 그 밖(펼쳐진 본문)에 있다.
    unfolded = re.sub(r"<details>.*?</details>", "", body, flags=re.S)
    assert "사용자 정의 커넥터" in unfolded


def test_onboarding_does_not_duplicate_the_client_tag_notice(client, monkeypatch):
    """`client=claude`를 다른 AI 이름으로 바꾸라는 안내는 이미 접속 주소 블록에
    있다 — 새 안내 블록이 같은 말을 한 번 더 하면 화면이 장황해진다."""
    _connect_via_login(client, monkeypatch, github_id=30004, repo="nick/memories")
    body = client.get("/auth/me").text
    assert body.count("client=chatgpt") == 1


def test_pages_are_mobile_and_dark_mode_ready(client, monkeypatch):
    """모든 화면이 _html_page 하나를 쓰므로 여기 한 곳만 확인하면 된다."""
    _connect_via_login(client, monkeypatch, github_id=30005, repo="olga/memories")
    body = client.get("/auth/me").text

    assert 'name="viewport"' in body and "width=device-width" in body
    assert "prefers-color-scheme" in body
    # 공백 없는 긴 주소가 좁은 화면을 옆으로 밀어내지 않도록 줄바꿈 규칙이 있어야 한다.
    assert "word-break" in body or "overflow-wrap" in body


def test_login_required_page_also_uses_the_shared_shell():
    """로그인 안내처럼 세션 없는 화면도 같은 껍데기를 쓴다(한 곳만 고치면 전부
    반영된다는 전제를 고정)."""
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    body = fresh_client.get("/auth/me").text
    assert 'name="viewport"' in body
    assert "prefers-color-scheme" in body


# ---------------------------------------------------------------------------
# 연결 시험(namu-60) — 판정은 반드시 세 갈래. "지금은 확인 불가"를 "죽었다"로
# 단정하면, 멀쩡한 주소를 쥔 사용자가 스스로 커넥터를 깨뜨린다.
# ---------------------------------------------------------------------------
# 세 판정을 서로 구분하는 문구. "주소가 잘못됐다"류의 조각으로 단언하면 "확인
# 불가" 안내문에 든 '주소가 잘못됐다는 뜻은 아닙니다'와 겹쳐 서로를 못 가른다.
_ALIVE_MARK = "살아있습니다"
_INVALID_MARK = "더 이상 유효하지 않습니다"
_UNKNOWN_MARK = "지금은 확인할 수 없습니다"


@pytest.fixture(autouse=True)
def _no_probe_retry_sleep(monkeypatch):
    """재시도 대기(1초)만 걷어낸다 — 재시도 자체는 그대로 일어난다(호출 횟수로
    검증한다)."""
    monkeypatch.setattr(wa, "_MCP_PROBE_RETRY_DELAY_SEC", 0)


def _fake_probe(*statuses):
    """`_http_probe` 대역. 호출된 URL을 기록하고 준 순서대로 상태코드를 돌려준다
    (마지막 값을 계속 반복)."""
    urls = []

    def _probe(url):
        urls.append(url)
        idx = min(len(urls) - 1, len(statuses) - 1)
        return statuses[idx]

    return _probe, urls


def test_connection_test_reports_alive(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=31001, repo="pat/memories")
    probe, urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test")

    assert r.status_code == 200
    assert _ALIVE_MARK in r.text
    assert _INVALID_MARK not in r.text
    assert _UNKNOWN_MARK not in r.text
    # 자기 자신을(컨테이너 내부 주소로) 그 사용자의 진짜 열쇠로 두드렸는지.
    assert len(urls) == 1
    assert urls[0].startswith("http://127.0.0.1:")
    assert urls[0].endswith(f"/mcp/{row['mcp_secret']}")


def test_connection_test_uses_the_configured_server_port(client, monkeypatch):
    """포트는 새 환경변수가 아니라 서버가 실제로 바인드하는 값(NAMU_HTTP_PORT)에서
    얻는다 — 엉뚱한 포트를 두드리면 멀쩡한 주소도 늘 '확인 불가'가 된다."""
    monkeypatch.setenv("NAMU_HTTP_PORT", "9911")
    _connect_via_login(client, monkeypatch, github_id=31002, repo="quinn/memories")
    probe, urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    client.post("/auth/mcp/test")

    assert urls[0].startswith("http://127.0.0.1:9911/mcp/")
    # 바깥 도메인으로 나가면 Cloudflare 터널을 한 바퀴 돌아야 하고 컨테이너
    # 안에서는 이름이 안 풀린다 — 절대 쓰지 않는다.
    assert "onnamu.kr" not in urls[0]
    assert "testserver" not in urls[0]


def test_self_probe_port_default_matches_the_port_the_server_binds(monkeypatch):
    """NAMU_HTTP_PORT가 비어 있을 때의 기본값이 routing_server.main()이 실제로
    uvicorn에 넘기는 기본값과 같아야 한다 — 한쪽만 바뀌면 연결 시험이 아무도
    없는 포트를 두드리며 조용히 '확인 불가'만 낸다."""
    import uvicorn

    monkeypatch.delenv("NAMU_HTTP_PORT", raising=False)
    monkeypatch.delenv("NAMU_HTTP_HOST", raising=False)
    captured = {}

    monkeypatch.setattr(rs, "build_app", lambda: object())
    monkeypatch.setattr(
        # **kwargs — main()이 넘기는 부가 인자(namu-67의 log_config 등)가 늘어도
        # 이 테스트의 관심사(host/port)와 무관하게 깨지지 않게 한다.
        uvicorn,
        "run",
        lambda app, host, port, **kwargs: captured.update(host=host, port=port),
    )

    rs.main()

    assert captured["port"] == wa._self_http_port()


def test_connection_test_reports_invalid_address_on_404(client, monkeypatch):
    """404는 문지기가 '장부에 없는 열쇠'라고 명시적으로 거절한 경우 — 이것만
    '주소가 잘못됨'이다."""
    _connect_via_login(client, monkeypatch, github_id=31003, repo="rita/memories")
    probe, urls = _fake_probe(404)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test")

    assert _INVALID_MARK in r.text
    assert _ALIVE_MARK not in r.text
    assert _UNKNOWN_MARK not in r.text
    assert len(urls) == 1  # 명확한 거절은 재시도하지 않는다


@pytest.mark.parametrize("first_status", [500, 502, 503, None])
def test_connection_test_retries_once_then_says_cannot_tell(client, monkeypatch, first_status):
    """5xx·타임아웃(None)은 짧게 한 번 재시도하고, 그래도 같으면 '지금은 확인
    불가'다 — **절대 죽었다고 단정하지 않는다.** 배포 직후의 일시 502를 실패로
    단정하면 사용자가 멀쩡한 주소를 재발급해 커넥터를 스스로 깨뜨린다."""
    _connect_via_login(client, monkeypatch, github_id=31004 + (first_status or 0), repo="sam/mem")
    probe, urls = _fake_probe(first_status)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test")

    assert len(urls) == 2, "일시적 실패인데 재시도하지 않았다"
    assert _UNKNOWN_MARK in r.text
    assert _INVALID_MARK not in r.text, "일시적 실패를 '주소가 잘못됨'으로 단정했다"
    assert _ALIVE_MARK not in r.text
    assert "잠깐" in r.text or "다시" in r.text  # 다시 눌러보라는 안내


def test_connection_test_recovers_on_retry(client, monkeypatch):
    """첫 시도가 502였어도 재시도가 정상이면 '살아있음'이어야 한다 — 재시도가
    판정에 실제로 반영되는지(형식만 갖춘 재시도가 아닌지) 확인한다."""
    _connect_via_login(client, monkeypatch, github_id=31009, repo="tom/memories")
    probe, urls = _fake_probe(502, 200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test")

    assert len(urls) == 2
    assert _ALIVE_MARK in r.text
    assert _UNKNOWN_MARK not in r.text


def test_connection_test_requires_post(client, monkeypatch):
    _connect_via_login(client, monkeypatch, github_id=31010, repo="uma/memories")
    probe, urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.get("/auth/mcp/test")

    assert r.status_code == 405
    assert urls == []


def test_connection_test_without_session_rejected(monkeypatch):
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    probe, urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = fresh_client.post("/auth/mcp/test")

    assert r.status_code == 401
    assert urls == [], "세션도 없는데 서버가 자기 자신을 두드렸다"


def test_connection_test_with_forged_session_rejected(monkeypatch):
    victim_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    row = _connect_via_login(
        victim_client, monkeypatch, github_id=31011, repo="victim/probe-repo"
    )
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    forged = f"{int(time.time()) + 1800}|{row['user_key']}.{'0' * 64}"
    fresh_client.cookies.set(wa._SESSION_COOKIE_NAME, forged)
    probe, urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = fresh_client.post("/auth/mcp/test")

    assert r.status_code == 401
    assert urls == []
    assert row["mcp_secret"] not in r.text


# ---------------------------------------------------------------------------
# 눌렀는지 알 수 있는가(namu-69) — 이 절이 지키는 것은 판정이 아니라 **전달**이다.
#
# 실측 보고: 사용자가 [연결 시험]을 누른 뒤 "아무 문구도 안 나온다"고 했고, 잠시
# 뒤 결과는 정상이었다. 이어서 "페이지가 새로고침되는 것처럼만 보이고 달라진 게
# 눈에 안 띈다"고 했다. 즉 기능은 내내 통과했는데 ①기다리는 동안 화면이 비어 있고
# ②결과가 나와도 화면 전체가 다시 그려져 무엇이 바뀌었는지 알 수 없었다. 이
# 버튼은 "지금 살아있는가"를 확인하려고 누르는 것이라, 무반응은 곧 "서버가 죽었나"로
# 읽힌다 — 기능의 목적과 정반대 인상을 준다.
# ---------------------------------------------------------------------------
def test_my_page_has_progress_and_result_slots_for_the_test_button(client, monkeypatch):
    """누르는 순간 채워질 '진행' 자리와 결과가 들어올 자리가 화면에 미리 있어야
    한다 — 그 자리가 없으면 결과는 갈 곳이 없어 페이지 전체 새로고침으로 돌아간다."""
    _connect_via_login(client, monkeypatch, github_id=31020, repo="wen/memories")

    body = client.get("/auth/me").text

    assert 'id="mcp-test-form"' in body
    assert 'id="mcp-test-btn"' in body
    assert 'id="mcp-test-progress"' in body
    assert 'id="mcp-test-result"' in body
    assert "확인하는 중입니다" in body


def test_test_button_is_locked_while_the_check_runs(client, monkeypatch):
    """응답을 기다리는 동안 버튼을 잠근다 — 십여 초짜리 확인이라 연타가 쉽고,
    겹친 요청은 서버가 자기 자신을 여러 번 두드리게 만든다. 끝나면 원래 이름표로
    되돌린다(잠긴 채로 남으면 두 번째 확인을 못 한다)."""
    _connect_via_login(client, monkeypatch, github_id=31025, repo="cho/memories")

    body = client.get("/auth/me").text

    assert "b.disabled=true" in body
    assert "b.disabled=false" in body
    assert "b.textContent=label" in body


def test_announced_wait_covers_the_real_worst_case(client, monkeypatch):
    """안내에 적힌 대기 시간은 실제 최악(프로브 2회 + 재시도 대기) 이상이어야
    한다. 손으로 적은 숫자는 타임아웃을 조정한 순간 조용히 거짓말이 되고, 그
    문구를 믿고 기다리는 사람에게는 그것이 곧 고장이다."""
    _connect_via_login(client, monkeypatch, github_id=31021, repo="xu/memories")

    body = client.get("/auth/me").text

    worst = wa._MCP_PROBE_TIMEOUT_SEC * 2 + wa._MCP_PROBE_RETRY_DELAY_SEC
    announced = int(re.search(r"최대 (\d+)초쯤", body).group(1))
    assert announced >= worst


def test_result_notice_is_visually_unmistakable(client, monkeypatch):
    """결과 상자는 색만으로 구분하지 않는다 — 아이콘과 배경으로 본문과 갈라지고,
    화면을 보지 않는 사용자에게도 등장이 읽혀야 한다(role=status)."""
    _connect_via_login(client, monkeypatch, github_id=31022, repo="yuna/memories")
    probe, _urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test", headers={"Accept": "application/json"})

    notice = r.json()["notice_html"]
    assert _ALIVE_MARK in notice
    assert "✅" in notice
    assert 'role="status"' in notice
    assert "background:" in notice


def test_in_place_request_returns_only_the_notice(client, monkeypatch):
    """화면 안에서 그 자리에 심으려는 요청에는 알림 상자만 돌려준다 — 페이지를
    통째로 돌려주면 그 안의 접속 주소·사용자 키까지 매번 함께 실려 나간다."""
    row = _connect_via_login(client, monkeypatch, github_id=31023, repo="zoe/memories")
    probe, _urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test", headers={"Accept": "application/json"})

    assert r.status_code == 200
    body = r.json()["notice_html"]
    assert "<html" not in body.lower()
    assert row["mcp_secret"] not in body


@pytest.mark.parametrize(
    "status,mark",
    [(200, _ALIVE_MARK), (404, _INVALID_MARK), (None, _UNKNOWN_MARK)],
)
def test_in_place_path_gives_the_same_three_verdicts(client, monkeypatch, status, mark):
    """포장지만 다를 뿐 판정은 같아야 한다 — 두 경로가 갈라지면 한쪽만 고치는
    사고가 난다(이 파일이 반복해서 지켜온 규약)."""
    _connect_via_login(
        client, monkeypatch, github_id=31024 + (status or 0), repo="amy/memories"
    )
    probe, _urls = _fake_probe(status)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test", headers={"Accept": "application/json"})

    assert mark in r.json()["notice_html"]


def test_in_place_request_without_session_still_says_something(monkeypatch):
    """세션이 끊긴 채 눌렀을 때 조용히 아무 일도 안 일어나면 사용자는 서버가
    죽었다고 읽는다 — 거절도 화면에 뜨는 한 줄로 돌려준다."""
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    probe, urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = fresh_client.post("/auth/mcp/test", headers={"Accept": "application/json"})

    assert r.status_code == 401
    assert "로그인" in r.json()["notice_html"]
    assert urls == [], "세션도 없는데 서버가 자기 자신을 두드렸다"


def test_test_button_still_works_without_javascript(client, monkeypatch):
    """자바스크립트가 없거나 실패하면 폼이 그대로 제출돼 종전처럼 페이지 전체가
    다시 그려지고 같은 결과가 나와야 한다 — 기능이 사라지면 안 된다."""
    _connect_via_login(client, monkeypatch, github_id=31030, repo="ben/memories")
    probe, _urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test")  # Accept 헤더 없음 = 평범한 폼 제출

    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert _ALIVE_MARK in r.text


# ---------------------------------------------------------------------------
# 재발급 / 폐기(namu-60)
#
# "옛 주소가 즉시 막힌다"는 단언은 장부만 보지 않고 **실제 앱에 요청을 넣어**
# 확인한다 — 라우팅 서버가 장부를 조회하는 구조라는 전제 자체가 깨지면(예:
# 어딘가에 열쇠를 캐시하기 시작하면) 장부 단언만으로는 아무것도 못 잡는다.
# ---------------------------------------------------------------------------
def _mcp_gate(secret: str) -> "tuple[int, dict]":
    """실제 문지기(`rs._PerUserSecretDispatcher`)에 `/mcp/<열쇠>`를 넣고
    `(상태코드, 통과했다면 안쪽 앱이 본 scope)`를 돌려준다.

    이것이 운영에서 "주소가 살았나 죽었나"를 실제로 판정하는 코드다 — 요청마다
    `identity.get_by_mcp_secret`으로 장부를 조회해 못 찾으면 404로 끊는다.

    안쪽 MCP 앱은 통과 여부만 보면 되므로 200을 돌려주는 대역으로 바꾼다.
    FastMCP 세션 매니저는 모듈 싱글턴이라 한 프로세스에서 lifespan을 두 번 열
    수 없고(그 한 번은 test_routing_server.py의 lifespan 스모크가 이미 쓴다),
    그렇다고 실제 MCP 응답 코드까지 이 테스트가 확인할 필요는 없다.
    (실서버 왕복 확인은 별도 스모크로 수행 — 유효 열쇠 200 / 폐기·재발급 후
    404가 실측됐다.)
    """
    seen: dict = {}

    async def _inner(scope, receive, send):
        seen["path"] = scope["path"]
        seen["query"] = scope["query_string"].decode("latin-1")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    status = asyncio.run(_raw_asgi_get(rs._PerUserSecretDispatcher(_inner), f"/mcp/{secret}"))
    return status, seen


def _ledger_row(user_key: str) -> dict:
    conn = identity.connect()
    try:
        return identity.get_by_user_key(conn, user_key)
    finally:
        conn.close()


def test_rotate_issues_new_address_and_shows_it_immediately(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=32001, repo="vera/memories")
    old_secret = row["mcp_secret"]

    r = client.post("/auth/mcp/rotate", data={"confirm": "yes"})

    assert r.status_code == 200
    new_secret = _ledger_row(row["user_key"])["mcp_secret"]
    assert new_secret != old_secret
    # 새 주소가 그 자리에서 완성형으로 보여야 한다(다시 찾아 헤매지 않도록).
    assert f"/mcp/{new_secret}?client=claude" in r.text
    assert old_secret not in r.text
    assert "새 주소를 발급했습니다" in r.text
    # 안내도 함께 보인다(내 페이지와 같은 화면을 쓴다).
    _assert_onboarding_guide(r.text)


def test_rotate_asks_once_before_breaking_the_address_you_are_using(client, monkeypatch):
    """재발급은 "새 주소가 생긴다"로 들리지만 실제 결과는 **쓰던 주소가 그 자리에서
    막히는 것**이다. 그냥 눌러 본 사람이 AI 커넥터를 스스로 끊게 되므로, 폐기와
    같은 문턱을 둔다(사용자 지적, 2026-08-02)."""
    row = _connect_via_login(client, monkeypatch, github_id=32011, repo="ian/memories")
    old_secret = row["mcp_secret"]

    r = client.post("/auth/mcp/rotate")

    assert r.status_code == 200
    # 확인 화면일 뿐, 아직 아무것도 바뀌지 않았다.
    assert _ledger_row(row["user_key"])["mcp_secret"] == old_secret
    assert _mcp_gate(old_secret)[0] == 200, "묻기만 했는데 옛 주소가 끊겼다"
    assert "새로 발급할까요" in r.text
    assert 'value="yes"' in r.text
    # 되돌아갈 길이 반드시 함께 있어야 한다.
    assert 'href="/auth/me"' in r.text


def test_rotate_does_not_ask_when_there_is_nothing_to_break(client, monkeypatch):
    """폐기해 둔 사람이 되돌리러 왔을 때는 끊길 연결 자체가 없다 — 그 자리에
    문턱을 세우면 그건 안전장치가 아니라 방해다."""
    row = _connect_via_login(client, monkeypatch, github_id=32012, repo="jane/memories")
    client.post("/auth/mcp/revoke", data={"confirm": "yes"})

    r = client.post("/auth/mcp/rotate")

    assert "새로 발급할까요" not in r.text
    new_secret = _ledger_row(row["user_key"])["mcp_secret"]
    assert new_secret and new_secret != row["mcp_secret"]


def test_rotate_blocks_the_old_address_on_the_real_gate(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=32002, repo="walt/memories")
    old_secret = row["mcp_secret"]
    assert _mcp_gate(old_secret)[0] == 200  # 재발급 전에는 통과한다

    client.post("/auth/mcp/rotate", data={"confirm": "yes"})
    new_secret = _ledger_row(row["user_key"])["mcp_secret"]

    assert _mcp_gate(old_secret)[0] == 404, "옛 주소가 여전히 통한다"
    status, seen = _mcp_gate(new_secret)
    assert status == 200
    # 새 열쇠가 같은 사람으로 판정되는지까지 — 열쇠만 바뀌고 서랍은 그대로여야 한다.
    assert f"user={row['user_key']}" in seen["query"]


def test_rotate_requires_post(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=32003, repo="xena/memories")

    r = client.get("/auth/mcp/rotate")

    assert r.status_code == 405
    assert _ledger_row(row["user_key"])["mcp_secret"] == row["mcp_secret"]


def test_rotate_without_session_rejected_and_changes_nothing(monkeypatch):
    victim_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    row = _connect_via_login(
        victim_client, monkeypatch, github_id=32004, repo="victim/rotate-repo"
    )
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")

    r = fresh_client.post("/auth/mcp/rotate")

    assert r.status_code == 401
    assert _ledger_row(row["user_key"])["mcp_secret"] == row["mcp_secret"]
    assert row["mcp_secret"] not in r.text


def test_revoke_first_press_only_asks_for_confirmation(client, monkeypatch):
    """확인 단계가 진짜로 있는지 — 첫 POST는 장부를 한 글자도 바꾸면 안 된다."""
    row = _connect_via_login(client, monkeypatch, github_id=32005, repo="yuri/memories")

    r = client.post("/auth/mcp/revoke")

    assert r.status_code == 200
    assert "정말" in r.text  # 확인을 묻는 화면
    assert _ledger_row(row["user_key"])["mcp_secret"] == row["mcp_secret"]


def test_revoke_confirmed_removes_the_address(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=32006, repo="zack/memories")
    secret = row["mcp_secret"]
    assert _mcp_gate(secret)[0] == 200  # 폐기 전에는 통과한다

    r = client.post("/auth/mcp/revoke", data={"confirm": "yes"})

    assert r.status_code == 200
    assert "폐기했습니다" in r.text
    assert _ledger_row(row["user_key"])["mcp_secret"] is None
    assert secret not in r.text
    # 실제 문지기에서도 그 주소가 막혔는지.
    assert _mcp_gate(secret)[0] == 404


def test_revoked_address_is_not_resurrected_by_visiting_my_page(client, monkeypatch):
    """내 페이지는 열쇠가 비어 있으면 발급해 주는 자가 치유 경로를 갖고 있다 —
    폐기한 사용자에게 그 경로가 돌면 폐기가 새로고침 한 번에 취소된다."""
    row = _connect_via_login(client, monkeypatch, github_id=32007, repo="amy/memories")
    client.post("/auth/mcp/revoke", data={"confirm": "yes"})

    r = client.get("/auth/me")

    assert r.status_code == 200
    assert _ledger_row(row["user_key"])["mcp_secret"] is None
    assert "폐기" in r.text  # 장애로 오해할 문구가 아니라 폐기 상태 안내
    assert "만들지 못했습니다" not in r.text
    # 없는 주소를 그럴듯하게 그리지 않는다(완성 주소도, 복사 상자도 없다).
    assert "?client=claude" not in r.text
    assert 'id="mcp-url"' not in r.text
    assert row["mcp_secret"] not in r.text


def test_revoked_user_can_get_a_new_address_by_rotating(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=32008, repo="ben/memories")
    client.post("/auth/mcp/revoke", data={"confirm": "yes"})

    r = client.post("/auth/mcp/rotate")

    assert r.status_code == 200
    new_secret = _ledger_row(row["user_key"])["mcp_secret"]
    assert new_secret and new_secret != row["mcp_secret"]
    assert f"/mcp/{new_secret}?client=claude" in r.text
    assert _mcp_gate(new_secret)[0] == 200


def test_revoke_requires_post(client, monkeypatch):
    row = _connect_via_login(client, monkeypatch, github_id=32009, repo="cara/memories")

    r = client.get("/auth/mcp/revoke")

    assert r.status_code == 405
    assert _ledger_row(row["user_key"])["mcp_secret"] == row["mcp_secret"]


def test_revoke_without_session_rejected_and_changes_nothing(monkeypatch):
    victim_client = TestClient(wa.build_auth_app(), base_url="https://testserver")
    row = _connect_via_login(
        victim_client, monkeypatch, github_id=32010, repo="victim/revoke-repo"
    )
    fresh_client = TestClient(wa.build_auth_app(), base_url="https://testserver")

    r = fresh_client.post("/auth/mcp/revoke", data={"confirm": "yes"})

    assert r.status_code == 401
    assert _ledger_row(row["user_key"])["mcp_secret"] == row["mcp_secret"]


def test_connection_test_on_revoked_account_says_no_address_to_test(client, monkeypatch):
    """폐기 상태에서 시험을 누르면 '주소가 잘못됨'이 아니라 '시험할 주소가
    없음'이어야 한다 — 두드릴 대상 자체가 없으므로 자기 호출도 하지 않는다."""
    _connect_via_login(client, monkeypatch, github_id=32011, repo="dana/memories")
    client.post("/auth/mcp/revoke", data={"confirm": "yes"})
    probe, urls = _fake_probe(200)
    monkeypatch.setattr(wa, "_http_probe", probe)

    r = client.post("/auth/mcp/test")

    assert r.status_code == 200
    assert "시험할 주소가 없습니다" in r.text
    assert urls == []


def test_mcp_management_routes_are_registered_in_build_auth_app():
    """'만들었다'와 '쓰인다'는 다르다 — 라우트가 실제로 앱에 붙어 있는지
    (405가 아니라 404가 나오면 등록 자체가 빠진 것) 경로 목록으로 확인한다."""
    paths = {
        (route.path, tuple(sorted(route.methods - {"HEAD"})))
        for route in wa.build_auth_app().routes
    }
    assert ("/auth/mcp/test", ("POST",)) in paths
    assert ("/auth/mcp/rotate", ("POST",)) in paths
    assert ("/auth/mcp/revoke", ("POST",)) in paths


# ---------------------------------------------------------------------------
# 기억 열람·검색 + 쪽지 떼기 (namu-60 완료조건 4·5)
#
# 저장소 왕복(user_repo.ensure_ready/push)은 걷어낸다 — 이 화면이 검증할 것은
# "무엇을 보여주고 무엇을 지우는가"이지 git 동작이 아니다. 다만 **push를 부르긴
# 하는지**는 반드시 본다: 안 부르면 다음 최신화 때 뗀 쪽지가 되살아난다.
# ---------------------------------------------------------------------------
def _memory_env(monkeypatch, tmp_path, user_key):
    """그 사용자의 기억 파일 자리를 만들고 저장소 왕복을 스텁으로 바꾼다.

    돌려주는 pushes 리스트가 비어 있으면 "이 서버에서만 지우고 회원 저장소에는
    반영하지 않았다"는 뜻이다(부활 사고의 정확한 재현 조건).
    """
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path / "store"))
    monkeypatch.setattr(
        wa.user_repo, "ensure_ready", lambda conn, key: wa.user_repo.user_dir(key)
    )
    pushes = []

    def _fake_push(conn, key, message=""):
        pushes.append((key, message))
        return True

    monkeypatch.setattr(wa.user_repo, "push", _fake_push)

    paths = wa._memory_paths(user_key)
    for p in (
        paths.learnings_yaml, paths.profile_yaml, paths.memo_yaml,
        paths.attachments_yaml, paths.db_path,
    ):
        p.parent.mkdir(parents=True, exist_ok=True)
    return paths, pushes


def test_memory_page_requires_login():
    c = TestClient(wa.build_auth_app(), base_url="https://testserver")
    r = c.get("/auth/memory")
    assert r.status_code == 401
    assert "로그인" in r.text


def test_memory_page_offers_four_bowls_including_tasks(client, monkeypatch, tmp_path):
    """그릇은 넷이다 — 작업일지도 포함된다.

    이 시험은 원래 "작업일지는 없어야 한다"고 반대로 못박고 있었다. 근거였던
    namu-68 격리 결정은 작업일지에 **쓰는** 것을 막은 것이고, 회원 저장소에는
    tasks/ 폴더가 통째로 올라와 서버 사본에 이미 들어 있다(2026-08-01 실측).
    """
    row = _connect_via_login(client, monkeypatch, github_id=41001, repo="ann/mem")
    _memory_env(monkeypatch, tmp_path, row["user_key"])

    r = client.get("/auth/memory")

    assert r.status_code == 200
    assert "교훈" in r.text
    assert "개인 사실" in r.text
    assert "쪽지" in r.text
    assert "작업일지" in r.text
    assert "bowl=tasks" in r.text


def test_memory_lists_learnings_summary_and_hides_body_behind_details(
    client, monkeypatch, tmp_path
):
    row = _connect_via_login(client, monkeypatch, github_id=41002, repo="bob/mem")
    paths, _ = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _cfg, db_mod, _memo, _profile = wa._core()
    db_mod.record(
        "작업하나",
        "success",
        "이유 문단입니다",
        paths=paths,
        summary="교훈요약마커",
        body="아주 긴 원문마커",
    )

    r = client.get("/auth/memory?bowl=learnings")

    assert r.status_code == 200
    assert "교훈요약마커" in r.text
    assert "이유 문단입니다" in r.text
    # 원문은 접혀 있어야 한다(목록에서 항목 하나가 화면을 다 먹지 않게)
    assert "<details>" in r.text
    assert "아주 긴 원문마커" in r.text


def test_memory_search_without_match_says_none_instead_of_showing_latest(
    client, monkeypatch, tmp_path
):
    """db.recall은 검색 결과가 없으면 최신 목록으로 물러난다 — 그대로 쓰면
    찾는 말이 없는데도 뭔가 나온 것처럼 보인다. 그 함정에 빠지지 않는지 본다."""
    row = _connect_via_login(client, monkeypatch, github_id=41003, repo="cat/mem")
    paths, _ = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _cfg, db_mod, _memo, _profile = wa._core()
    db_mod.record("작업하나", "success", "이유", paths=paths, summary="사과에관한교훈")

    r = client.get("/auth/memory", params={"bowl": "learnings", "q": "존재하지않는낱말zzz"})

    assert r.status_code == 200
    assert "사과에관한교훈" not in r.text
    assert "없습니다" in r.text


def test_memo_bowl_shows_remove_form_but_learnings_and_profile_do_not(
    client, monkeypatch, tmp_path
):
    """그릇마다 되는 동작이 다르다 — 지울 수 있는 그릇에만 폼을 그린다.
    설명만 다르고 버튼은 다 있으면 눌러 보고 나서야 알게 된다."""
    row = _connect_via_login(client, monkeypatch, github_id=41004, repo="dan/mem")
    paths, _ = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _cfg, db_mod, memo, profile = wa._core()
    memo.add(paths=paths, summary="쪽지요약마커", reason="왜", body="본문")
    db_mod.record("작업하나", "success", "이유", paths=paths, summary="교훈요약마커")
    profile.record_fact("주제", paths=paths, summary="사실요약마커", reason="왜", body="본문")

    memo_page = client.get("/auth/memory?bowl=memo")
    learn_page = client.get("/auth/memory?bowl=learnings")
    prof_page = client.get("/auth/memory?bowl=profile")

    assert "쪽지요약마커" in memo_page.text
    assert 'action="/auth/memo/remove"' in memo_page.text
    assert 'name="memo_id"' in memo_page.text

    assert "교훈요약마커" in learn_page.text
    assert "/auth/memo/remove" not in learn_page.text
    assert "사실요약마커" in prof_page.text
    assert "/auth/memo/remove" not in prof_page.text


def test_memo_remove_deletes_from_file_and_pushes_so_it_cannot_come_back(
    client, monkeypatch, tmp_path
):
    """완료조건 5의 핵심 — 뗀 쪽지가 파일에서 실제로 사라지고, 그 결과가 회원
    저장소로 밀려나간다. push를 빠뜨리면 다음 최신화에 그대로 부활한다."""
    row = _connect_via_login(client, monkeypatch, github_id=41005, repo="eve/mem")
    paths, pushes = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _cfg, _db, memo, _profile = wa._core()
    keep_id = memo.add(paths=paths, summary="남길쪽지", reason="왜", body="본문")
    drop_id = memo.add(paths=paths, summary="뗄쪽지", reason="왜", body="본문")

    r = client.post("/auth/memo/remove", data={"memo_id": drop_id})

    assert r.status_code == 200
    remaining = {e.get("id") for e in memo.load_all(paths=paths)}
    assert drop_id not in remaining
    assert keep_id in remaining
    assert "뗐습니다" in r.text
    assert "뗄쪽지" not in r.text
    assert "남길쪽지" in r.text
    assert len(pushes) == 1 and pushes[0][0] == row["user_key"]


def test_memo_remove_can_delete_several_at_once(client, monkeypatch, tmp_path):
    row = _connect_via_login(client, monkeypatch, github_id=41006, repo="fay/mem")
    paths, pushes = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _cfg, _db, memo, _profile = wa._core()
    ids = [
        memo.add(paths=paths, summary=f"쪽지{i}", reason="왜", body="본문")
        for i in range(3)
    ]

    r = client.post("/auth/memo/remove", data={"memo_id": [ids[0], ids[2]]})

    assert r.status_code == 200
    remaining = {e.get("id") for e in memo.load_all(paths=paths)}
    assert remaining == {ids[1]}
    assert len(pushes) == 1


def test_memo_remove_without_selection_changes_nothing_and_does_not_push(
    client, monkeypatch, tmp_path
):
    row = _connect_via_login(client, monkeypatch, github_id=41007, repo="gil/mem")
    paths, pushes = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _cfg, _db, memo, _profile = wa._core()
    memo.add(paths=paths, summary="그대로남는쪽지", reason="왜", body="본문")

    r = client.post("/auth/memo/remove", data={})

    assert r.status_code == 200
    assert "고르지 않으셨습니다" in r.text
    assert len(memo.load_all(paths=paths)) == 1
    assert pushes == []


def test_memo_remove_rejects_missing_session_and_get_method(monkeypatch, tmp_path):
    """파괴적 동작이라 남의 요청(세션 없음)도, 링크 클릭(GET)도 통과하면 안 된다."""
    c = TestClient(wa.build_auth_app(), base_url="https://testserver")
    assert c.post("/auth/memo/remove", data={"memo_id": "x"}).status_code == 401
    assert c.get("/auth/memo/remove").status_code == 405


# ---------------------------------------------------------------------------
# 열린 작업 보드 (namu-60 완료조건 7)
#
# 확인할 것은 "무엇이 열린 작업이고 어떤 순서로 서는가"를 이 화면이 **스스로
# 정하지 않는다**는 점이다 — 판정은 코어(task_resolve)가 하고 웹은 옮겨 담기만
# 한다. 그래서 시험도 코어가 닫힘으로 보는 모양(log의 [완료])·책갈피 파일을
# 실물 그대로 만들어 넣는다.
# ---------------------------------------------------------------------------
def _make_task(user_key, project, slug, *, log_lines, title=None):
    """회원 저장소 사본에 작업 폴더 하나(task.md + log.md)를 만든다."""
    task_dir = wa.user_repo.user_dir(user_key) / "tasks" / project / slug
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.md").write_text(
        f"# {slug} — {title or slug}\n", encoding="utf-8"
    )
    (task_dir / "log.md").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return task_dir


def test_task_board_shows_open_tasks_with_next_line_and_omits_closed_ones(
    client, monkeypatch, tmp_path
):
    """보드의 주인공은 제목이 아니라 '다음에 할 일'이다 — 그 줄이 없으면 어디서
    이어서 할지 모르므로 화면이 있으나 마나다. 닫힌 작업은 빠져야 한다."""
    row = _connect_via_login(client, monkeypatch, github_id=41010, repo="ann/board")
    _memory_env(monkeypatch, tmp_path, row["user_key"])
    _make_task(
        row["user_key"],
        "myproj",
        "task-open",
        title="열린작업제목마커",
        log_lines=[
            "[시작] 2026-07-30 10:00:00 hp · 착수",
            "[다음] 2026-07-30 11:00:00 hp · 다음할일마커부터",
            "    왜: 왜여기서부터마커",
        ],
    )
    _make_task(
        row["user_key"],
        "myproj",
        "task-closed",
        title="닫힌작업제목마커",
        log_lines=[
            "[시작] 2026-07-29 10:00:00 hp · 착수",
            "[완료] 2026-07-29 12:00:00 hp · 끝",
        ],
    )

    r = client.get("/auth/memory?bowl=tasks")

    assert r.status_code == 200
    assert "열린작업제목마커" in r.text
    assert "다음할일마커부터" in r.text
    assert "왜여기서부터마커" in r.text
    assert "닫힌작업제목마커" not in r.text


def test_task_board_puts_pinned_task_first(client, monkeypatch, tmp_path):
    """책갈피가 꽂힌 작업이 맨 위에 선다 — 최근 활동순 하나로만 세우면 만든
    순서가 곧 중요도가 된다(코어 namu-70이 흡수한 규칙과 같은 결과여야 한다)."""
    row = _connect_via_login(client, monkeypatch, github_id=41011, repo="bob/board")
    _memory_env(monkeypatch, tmp_path, row["user_key"])
    _make_task(
        row["user_key"],
        "myproj",
        "task-recent",
        title="최근작업마커",
        log_lines=["[다음] 2026-07-31 23:00:00 hp · 나중"],
    )
    _make_task(
        row["user_key"],
        "myproj",
        "task-pinned",
        title="책갈피작업마커",
        log_lines=["[다음] 2026-07-20 09:00:00 hp · 먼저"],
    )
    pin = wa.user_repo.user_dir(row["user_key"]) / "tasks" / "myproj" / ".pin.hp"
    pin.write_text("task-pinned\n2026-08-01 10:00:00\n", encoding="utf-8")

    r = client.get("/auth/memory?bowl=tasks")

    assert r.status_code == 200
    assert r.text.index("책갈피작업마커") < r.text.index("최근작업마커")
    assert "📌" in r.text


def test_task_board_search_narrows_instead_of_showing_everything(
    client, monkeypatch, tmp_path
):
    row = _connect_via_login(client, monkeypatch, github_id=41012, repo="cat/board")
    _memory_env(monkeypatch, tmp_path, row["user_key"])
    _make_task(
        row["user_key"],
        "myproj",
        "task-apple",
        title="사과작업마커",
        log_lines=["[다음] 2026-07-30 11:00:00 hp · 사과를 깎는다"],
    )
    _make_task(
        row["user_key"],
        "myproj",
        "task-pear",
        title="배작업마커",
        log_lines=["[다음] 2026-07-30 12:00:00 hp · 배를 깎는다"],
    )

    hit = client.get("/auth/memory", params={"bowl": "tasks", "q": "사과작업마커"})
    miss = client.get("/auth/memory", params={"bowl": "tasks", "q": "없는낱말zzz"})

    assert "사과작업마커" in hit.text
    assert "배작업마커" not in hit.text
    assert "없습니다" in miss.text


def test_task_board_without_tasks_folder_says_none_instead_of_failing(
    client, monkeypatch, tmp_path
):
    """작업 기록을 한 번도 올린 적 없는 회원 — 폴더 자체가 없다. 오류가 아니라
    '아직 없음'이며, 안내는 웹에서 할 수 없는 일(대화로 남기기)을 시키지 않는다."""
    row = _connect_via_login(client, monkeypatch, github_id=41013, repo="dan/board")
    _memory_env(monkeypatch, tmp_path, row["user_key"])

    r = client.get("/auth/memory?bowl=tasks")

    assert r.status_code == 200
    assert "열려 있는 작업이 없습니다" in r.text


def test_task_board_reads_only_its_own_owner_folder(client, monkeypatch, tmp_path):
    """남의 작업 기록이 섞여 보이면 안 된다 — 사용자별 뿌리로 읽는지 확인한다."""
    row = _connect_via_login(client, monkeypatch, github_id=41014, repo="eve/board")
    _memory_env(monkeypatch, tmp_path, row["user_key"])
    _make_task(
        row["user_key"],
        "myproj",
        "mine",
        title="내작업마커",
        log_lines=["[다음] 2026-07-30 11:00:00 hp · 내 다음 할 일"],
    )
    _make_task(
        "someone-else",
        "myproj",
        "theirs",
        title="남의작업마커",
        log_lines=["[다음] 2026-07-31 11:00:00 hp · 남의 다음 할 일"],
    )

    r = client.get("/auth/memory?bowl=tasks")

    assert "내작업마커" in r.text
    assert "남의작업마커" not in r.text


def test_task_board_is_read_only_and_offers_no_remove_form(
    client, monkeypatch, tmp_path
):
    """쓰기는 이 서비스 전체에서 막혀 있다(namu-68) — 보드에도 손대는 버튼이
    있으면 안 된다."""
    row = _connect_via_login(client, monkeypatch, github_id=41015, repo="fox/board")
    _memory_env(monkeypatch, tmp_path, row["user_key"])
    _make_task(
        row["user_key"],
        "myproj",
        "mine",
        title="내작업마커",
        log_lines=["[다음] 2026-07-30 11:00:00 hp · 다음"],
    )

    r = client.get("/auth/memory?bowl=tasks")

    assert "/auth/memo/remove" not in r.text
    assert 'type="checkbox"' not in r.text


def test_core_still_exposes_the_timestamp_helper_the_board_depends_on():
    """보드 정렬은 코어의 비공개 함수(_latest_log_ts)를 부른다 — 규칙을 베끼지
    않으려는 의도적 선택이라, 코어를 올릴 때 이름이 사라지면 여기서 먼저 걸려야
    한다(화면이 조용히 엉뚱한 순서로 서는 것보다 낫다)."""
    assert callable(getattr(wa._core_tasks(), "_latest_log_ts", None))


def test_memory_routes_are_registered_in_build_auth_app():
    paths = {
        (route.path, tuple(sorted(route.methods - {"HEAD"})))
        for route in wa.build_auth_app().routes
    }
    assert ("/auth/memory", ("GET",)) in paths
    assert ("/auth/memo/remove", ("POST",)) in paths


# ---------------------------------------------------------------------------
# 첨부 기록 화면 (2026-08-07 사용자 요구 — 어떤 파일이 어떤 경로로 오갔는지)
# ---------------------------------------------------------------------------


def _seed_attachment(paths, path, status, **kwargs):
    attachments = wa._core_attachments()
    base = dict(
        summary="설계 문서", reason="파일째 남긴다", body="원문", paths=paths,
    )
    base.update(kwargs)
    return attachments.record_attachment(
        path=path, bytes_=base.pop("bytes_", 284915), status=status, **base
    )


def test_attachments_tab_shows_path_status_and_size(client, monkeypatch, tmp_path):
    """이 화면을 보는 이유가 '무슨 파일이 있더라'이므로 경로가 반드시 보여야 하고,
    크기는 사람이 읽는 단위로 나와야 한다(284915라고 적으면 큰지 알 수 없다)."""
    row = _connect_via_login(client, monkeypatch, github_id=41020, repo="eve/att")
    paths, _ = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _seed_attachment(paths, "attach_file/설계마커.pdf", "올림", topic="namu-70")

    r = client.get("/auth/memory?bowl=attachments")

    assert r.status_code == 200
    assert "attach_file/설계마커.pdf" in r.text
    assert "올림" in r.text
    assert "278 KB" in r.text
    assert "namu-70" in r.text


def test_attachments_tab_is_listed_and_labelled(client, monkeypatch, tmp_path):
    row = _connect_via_login(client, monkeypatch, github_id=41021, repo="eve/tab")
    _memory_env(monkeypatch, tmp_path, row["user_key"])

    r = client.get("/auth/memory?bowl=learnings")

    assert 'href="/auth/memory?bowl=attachments"' in r.text
    assert "첨부 기록" in r.text


def test_attachments_tab_keeps_showing_removed_files(client, monkeypatch, tmp_path):
    """지운 파일도 목록에 남아야 한다 — 이 화면을 보는 이유의 절반이 '그 파일
    어디 갔지'이고, 살아 있는 것만 보이면 그 질문에 답할 수 없다."""
    row = _connect_via_login(client, monkeypatch, github_id=41022, repo="eve/gone")
    paths, _ = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _seed_attachment(paths, "attach_file/사라진마커.pdf", "올림")
    _seed_attachment(
        paths, "attach_file/사라진마커.pdf", "지움", reason="왜뺐는지마커",
    )

    r = client.get("/auth/memory?bowl=attachments")

    assert "attach_file/사라진마커.pdf" in r.text
    assert "지움" in r.text
    assert "왜뺐는지마커" in r.text


def test_attachments_search_matches_the_file_name(client, monkeypatch, tmp_path):
    """다시 찾을 때 사람이 기억하는 것은 내용 설명이 아니라 파일 이름이다."""
    row = _connect_via_login(client, monkeypatch, github_id=41023, repo="eve/find")
    paths, _ = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _seed_attachment(paths, "attach_file/사과마커.pdf", "올림")
    _seed_attachment(paths, "attach_file/배마커.pdf", "올림")

    hit = client.get("/auth/memory", params={"bowl": "attachments", "q": "사과마커"})
    miss = client.get("/auth/memory", params={"bowl": "attachments", "q": "없는낱말zzz"})

    assert "사과마커" in hit.text
    assert "배마커" not in hit.text
    assert "없습니다" in miss.text


def test_attachments_tab_has_no_remove_form(client, monkeypatch, tmp_path):
    """첨부 기록은 지워지지 않는 기억이다 — 파일을 빼는 일과 기록을 지우는 일은
    다르며, 화면에 지우기 버튼이 있으면 둘이 같은 일로 보인다."""
    row = _connect_via_login(client, monkeypatch, github_id=41024, repo="eve/noform")
    paths, _ = _memory_env(monkeypatch, tmp_path, row["user_key"])
    _seed_attachment(paths, "attach_file/a.pdf", "올림")

    r = client.get("/auth/memory?bowl=attachments")

    assert "/auth/memo/remove" not in r.text
    assert 'type="checkbox"' not in r.text


def test_attachments_tab_without_any_file_says_none(client, monkeypatch, tmp_path):
    row = _connect_via_login(client, monkeypatch, github_id=41025, repo="eve/empty")
    _memory_env(monkeypatch, tmp_path, row["user_key"])

    r = client.get("/auth/memory?bowl=attachments")

    assert r.status_code == 200
    assert "아직 올리신 파일이 없습니다" in r.text


# ---------------------------------------------------------------------------
# 글꼴 파일 내보내기 (namu-75)
#
# 이 앱이 파일을 그대로 내보내는 유일한 자리다. 목록에 적힌 것만 나가야 하고,
# 목록과 실제 라우트가 어긋나면 글꼴만 조용히 404가 된다(화면은 시스템 글꼴로
# 계속 떠서 아무도 모른다) — 그래서 여기서 못박는다.
# ---------------------------------------------------------------------------
def test_every_listed_font_file_is_actually_served(client):
    assert ui.ASSET_PATHS, "내보낼 파일 목록이 비었다"
    for path in ui.ASSET_PATHS:
        r = client.get(path)
        assert r.status_code == 200, f"{path}가 안 나온다"
        assert r.headers["content-type"] == "font/woff2"
        # 파일 이름이 곧 내용이므로 다시 물어볼 이유가 없다.
        assert "immutable" in r.headers["cache-control"]
        # woff2 파일의 첫 네 글자는 언제나 'wOF2'다 — 엉뚱한 파일이 아닌지.
        assert r.content[:4] == b"wOF2"


def test_font_files_exist_in_the_repo(client):
    """파일이 저장소에 없으면 404가 되는데, 화면은 시스템 글꼴로 멀쩡히 떠서
    배포하고 한참 뒤에야 눈치챈다. 이미지에 안 들어간 경우도 같은 모양이다."""
    for path in ui.ASSET_PATHS:
        assert (wa._ASSET_DIR / Path(path).name).is_file(), f"{path} 파일이 없다"


def test_font_route_does_not_open_the_folder_underneath(client):
    """폴더를 통째로 여는 것(StaticFiles)이 아니라 목록에 적은 것만 연다 —
    같은 폴더의 사용 조건 파일조차 주소로는 나가지 않아야 한다."""
    assert (wa._ASSET_DIR / "OFL.txt").is_file(), "사용 조건 원문을 같이 두어야 한다"

    for path in ["/asset/OFL.txt", "/asset/../web_auth.py", "/asset/"]:
        assert client.get(path).status_code == 404, f"{path}가 열렸다"
