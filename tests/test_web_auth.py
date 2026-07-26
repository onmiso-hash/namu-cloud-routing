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
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

import identity
import routing_server as rs
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
        if url.startswith("https://api.github.com/user/installations/"):
            qs = parse_qs(urlparse(url).query)
            page = int(qs.get("page", ["1"])[0])
            per_page = int(qs.get("per_page", ["30"])[0])
            start = (page - 1) * per_page
            page_items = all_repos[start : start + per_page]
            total = repos_total_count if repos_total_count is not None else len(all_repos)
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
def test_callback_login_only_return_shows_install_and_prefilled_create_links(client, monkeypatch):
    state = _do_login(client)
    fake, calls = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})

    assert r.status_code == 200
    body = r.text
    # 설치 링크 — GitHub 설치 주소로 직접 걸면 state 쿠키를 심지 못해 설치 후
    # 콜백이 400으로 거절된다(2026-07-26 실측). 반드시 우리 경로를 경유한다.
    assert 'href="/auth/github/install"' in body
    assert "https://github.com/apps/" not in body
    # 미리 채운 저장소 생성 링크 — 계약값 리터럴로 확인
    assert "https://github.com/new?" in body
    assert "name=namu-memory" in body
    assert "visibility=private" in body
    # installation_id가 없었으니 저장소 목록 조회는 아예 없었어야 한다.
    assert not any(c["url"].startswith("https://api.github.com/user/installations/") for c in calls)


def test_callback_session_cookie_set_after_login(client, monkeypatch):
    state = _do_login(client)
    fake, _ = _make_fake_http_json()
    monkeypatch.setattr(wa, "_http_json", fake)

    r = client.get("/auth/github/callback", params={"state": state, "code": "goodcode"})
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


def test_build_app_mcp_route_still_requires_token(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_TOKEN", "supersecrettoken")
    app = rs.build_app()
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/mcp")
    assert r.status_code == 401


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
    `/auth/`로 시작하지 않으므로 auth_app을 절대 타면 안 된다(401이어야 함,
    200이면 우회 성공 = 회귀)."""
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_TOKEN", "supersecrettoken")
    app = rs.build_app()
    status = asyncio.run(_raw_asgi_get(app, "/mcp/../auth/github/login"))
    assert status == 401


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
