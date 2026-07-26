"""GitHub OAuth 사용자 로그인 흐름 (사용자 신원 계층 2차).

1차(github_app.py/identity.py)가 "나무 서버가 그 앱임을 증명하는" 서버 대 서버
인증을 끝냈다면, 이 모듈은 "이 브라우저를 쥔 사람이 누구인지"를 알아내는
사용자 대 서버 인증(OAuth)이다. 라우트 4개(`/auth/github/login` →
`/auth/github/callback` → 앱 설치가 필요하면 `/auth/github/install`을 거쳐
다시 `/auth/github/callback` → 저장소가 여럿이면 `/auth/github/select-repo`)로
구성된다.

설계 전제(핵심 — routing_server.py/github_app.py 모듈 docstring과 동일한 원칙):
  - 사용자 access token은 **저장하지 않는다**. 콜백 요청 처리 중 지역 변수로만
    쓰고 응답을 만들면 버려진다 — 영구 열쇠를 보관하지 않는 것이 이 설계
    전체의 존재 이유다.
  - 로그인 자체는 "신원 확인"일 뿐 저장소 접근권을 주지 않는다. 저장소 접근권은
    사용자가 별도로 앱을 설치(installation)하고 repo를 고를 때만 생긴다.
  - 이 모듈은 MCP 접속 주소(path_secret 포함)를 다루지 않는다 — 그 화면은
    namu-60 대시보드 소관이다. 여기서는 user_key와 연결 저장소명까지만 보여준다.

쿠키 서명 규약(자체 구현, 외부 라이브러리 미사용):
  `value.hexhmac` 형태(HMAC-SHA256, `NAMU_SESSION_SECRET` 키). 만료가 필요한
  값(state, session)은 서명 전에 `"<만료epoch>|<원래값>"`으로 감싸 서버가 직접
  만료를 강제한다(Max-Age는 클라이언트가 지키는 값이라 신뢰하지 않는다).
  `NAMU_HTTP_TOKEN`(MCP 헤더 인증 토큰)과 이 시크릿을 절대 겸용하지 않는다 —
  하나를 교체할 때 다른 하나가 함께 무효화되는 결합을 피하기 위해서다.

환경변수는 github_app.py/identity.py와 동일하게 전부 **호출 시점에 읽는다**
(모듈 로드 시점 상수로 굳히지 않는다 — 테스트가 monkeypatch.setenv로 격리해야
한다).
"""
import hashlib
import hmac
import html
import logging
import os
import secrets
import time
from contextlib import closing
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

import github_app as ga
import identity

logger = logging.getLogger("namu.web_auth")

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_USER_INSTALLATIONS_REPOS_URL_TMPL = (
    "https://api.github.com/user/installations/{iid}/repositories"
)
GITHUB_INSTALL_URL_TMPL = "https://github.com/apps/{slug}/installations/new"

_STATE_COOKIE_NAME = "namu_oauth_state"
_SESSION_COOKIE_NAME = "namu_session"
# state는 로그인 왕복(브라우저→GitHub→콜백) 동안만 살아있으면 된다 — 짧을수록
# CSRF에 악용될 수 있는 창이 좁아진다.
_STATE_COOKIE_TTL_SEC = 600
# 콜백 직후 저장소가 2개 이상이라 select-repo로 한 번 더 왕복해야 하는 경우까지
# 감안한 여유(사용자가 화면을 보고 고르는 시간 포함).
_SESSION_COOKIE_TTL_SEC = 1800

_DEFAULT_APP_SLUG = "namu-memory-app"

_HTTP_TIMEOUT_SEC = 15.0


# ---------------------------------------------------------------------------
# 환경변수 (지연 평가)
# ---------------------------------------------------------------------------
def _required_env(name: str, what: str) -> str:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name} 환경변수가 설정되지 않았습니다 — {what}")
    return raw


def _session_secret() -> bytes:
    """쿠키 서명·링크 서명 전용 키. NAMU_HTTP_TOKEN과 값을 공유하지 않는다.

    (재사용하면 MCP 접근 토큰을 교체할 때 로그인 중인 모든 세션·state 쿠키가
    함께 무효화되고, 반대로 세션 시크릿을 교체하면 MCP 인증이 끊긴다 — 서로
    다른 수명 주기를 갖는 값을 하나로 묶으면 안 된다.)
    """
    return _required_env(
        "NAMU_SESSION_SECRET",
        "쿠키 서명(state/session)과 select-repo 링크 서명에 쓸 임의의 강한 문자열을 "
        "지정하세요. NAMU_HTTP_TOKEN(MCP 헤더 인증 토큰)과 같은 값을 쓰지 마세요 — "
        "하나를 교체하면 다른 하나도 함께 무효화됩니다. "
        "Missing NAMU_SESSION_SECRET: set a strong random string, distinct from "
        "NAMU_HTTP_TOKEN.",
    ).encode("utf-8")


def _app_slug() -> str:
    """설치 링크(`github.com/apps/<slug>/installations/new`) 조립용."""
    return os.environ.get("NAMU_GITHUB_APP_SLUG", "").strip() or _DEFAULT_APP_SLUG


# ---------------------------------------------------------------------------
# 서명 — HMAC-SHA256, 값 자체는 로그/예외에 남기지 않는다(서명 실패도 이유만
# 알리고 후보 값은 담지 않는다).
# ---------------------------------------------------------------------------
def _hmac_hex(payload: str) -> str:
    return hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _sign(value: str) -> str:
    return f"{value}.{_hmac_hex(value)}"


def _unsign(signed: "str | None") -> "str | None":
    if not signed or "." not in signed:
        return None
    value, _, mac = signed.rpartition(".")
    if not hmac.compare_digest(mac, _hmac_hex(value)):
        return None
    return value


def _sign_with_expiry(payload: str, ttl_seconds: int) -> str:
    expires_at = int(time.time()) + ttl_seconds
    return _sign(f"{expires_at}|{payload}")


def _unsign_with_expiry(signed: "str | None") -> "str | None":
    """검증 + 만료 확인. 서명이 틀렸거나 기한이 지났으면 이유를 구분하지 않고
    None을 돌려준다 — 호출부가 어느 쪽이든 "다시 로그인하라"로 처리하면 된다."""
    raw = _unsign(signed)
    if raw is None or "|" not in raw:
        return None
    expires_raw, _, payload = raw.partition("|")
    try:
        expires_at = int(expires_raw)
    except ValueError:
        return None
    if time.time() > expires_at:
        return None
    return payload


def _repo_link_sig(user_key: str, installation_id: int, repo: str) -> str:
    """콜백이 조회한 저장소 목록을 select-repo 링크에 실을 때 붙이는 서명.

    user_key까지 서명 대상에 포함하는 이유: repo/installation_id만 서명하면,
    한 사용자에게 발급된 select-repo 링크(예: 로그가 유출됐거나 화면을 같이
    본 경우)를 다른 로그인 세션이 그대로 재사용해 "남의 installation을 내
    계정에 연결"할 수 있다 — installation_id는 어느 GitHub 계정이 설치했는지와
    묶여 있으므로, 그 결과 나무가 다른 사람의 repo에 접근할 때 엉뚱한 사용자의
    토큰 경로로 라우팅되는 사고가 된다. user_key를 서명에 묶으면 이 링크는
    발급받은 세션 본인만 소비할 수 있다.
    """
    payload = f"{user_key}:{installation_id}:{repo}"
    return _hmac_hex(payload)


# ---------------------------------------------------------------------------
# HTTP — 이 모듈의 유일한 네트워크 접점. 테스트는 이 함수 하나만 monkeypatch하면
# 로그인→콜백→저장소 선택 전 흐름을 네트워크 없이 검증할 수 있다
# (github_app._post_json과 같은 설계 원칙).
#
# github_app._post_json과 달리 상태코드로 성패를 가르지 않고 (status, json)을
# 그대로 돌려준다 — GitHub 토큰 교환 엔드포인트(`/login/oauth/access_token`)는
# 실패해도 HTTP 200에 `{"error": ...}`로 응답하므로, 판정은 반드시 응답 본문을
# 본 호출부가 내려야 한다.
# ---------------------------------------------------------------------------
def _http_json(
    method: str, url: str, *, headers: "dict | None" = None, json_body: "dict | None" = None
) -> "tuple[int, dict]":
    import httpx

    resp = httpx.request(method, url, headers=headers or {}, json=json_body, timeout=_HTTP_TIMEOUT_SEC)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, (data if isinstance(data, dict) else {})


def _bearer_headers(user_token: str) -> dict:
    return {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _exchange_code_for_token(code: str) -> str:
    """authorization code를 사용자 access token으로 교환한다.

    확신 없는 지점(GitHub API 동작을 기억으로 단정): token 엔드포인트가 JSON
    본문(Content-Type: application/json)도 accept하는지는 공식 문서 기억에
    기반한 가정이며, 실제 호출로 확인하지 못했다 — 실패 시 대안은 폼 인코딩
    (application/x-www-form-urlencoded)으로 바꾸는 것이다.
    """
    status, data = _http_json(
        "POST",
        GITHUB_TOKEN_URL,
        headers={"Accept": "application/json"},
        json_body={
            "client_id": ga.client_id(),
            "client_secret": ga.client_secret(),
            "code": code,
        },
    )
    # GitHub은 실패해도 HTTP 200 + {"error": ...}로 응답할 수 있으므로 status만
    # 보면 안 된다 — 본문의 error 키를 반드시 함께 검사한다.
    error = data.get("error")
    if error or status >= 400:
        raise ValueError(
            f"GitHub 로그인 승인 교환에 실패했습니다 (error={error or 'unknown'}, "
            f"status={status}) — 처음부터 다시 로그인해 주세요. "
            "GitHub OAuth code exchange failed."
        )
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError(
            "GitHub 응답에 access_token이 없습니다. "
            "Missing access_token in GitHub response."
        )
    return token


def _fetch_github_user(user_token: str) -> dict:
    status, data = _http_json("GET", GITHUB_USER_URL, headers=_bearer_headers(user_token))
    if status >= 400:
        raise RuntimeError(f"GitHub 사용자 정보 조회에 실패했습니다 (status={status}).")
    return data


# 페이지당 최대(GitHub 허용 상한)로 받아 왕복 횟수를 줄인다. 안전 상한
# (page cap)은 무한 루프 방어이자 "이 개수를 넘는 계정은 실제로 없을 것"이라는
# 낙관이 아니라 최후의 방어선이다 — 도달하면 반드시 화면에 알린다(아래
# `_fetch_installation_repos`의 반환값 두 번째 요소 `truncated` 참고).
_INSTALLATION_REPOS_PER_PAGE = 100
_INSTALLATION_REPOS_MAX_PAGES = 50  # per_page(100) 기준 최대 5000개까지 수집


def _fetch_installation_repos(user_token: str, installation_id: int) -> "tuple[list[str], bool]":
    """installation이 허용한 저장소들의 `owner/repo` 목록을, 페이지네이션을 끝까지
    따라가며 전부 모은다.

    응답 스키마(`total_count`/`repositories`, 각 저장소의 `full_name`)와 페이지네이션
    기본값(`per_page=30`, `page=1`)은 GitHub 공식 OpenAPI 스펙
    (github/rest-api-description repo의 api.github.com.json,
    `GET /user/installations/{installation_id}/repositories`)으로 확정된 사실이다
    — 기억에 의존한 추정이 아니다.

    반환값 `(names, truncated)`: `truncated=True`면 안전 상한
    (`_INSTALLATION_REPOS_MAX_PAGES` 페이지)에 걸려 전부 못 가져왔다는 뜻이다.
    설치 시 "All repositories"를 고른 계정이 저장소를 30개(GitHub page 기본값)
    넘게 갖고 있으면 첫 페이지만 받고 끝나 뒤쪽이 에러 없이 조용히 누락되는
    결함이 있었다(실제 코드 결함으로 지적됨) — 그래서 이 함수는 반드시 페이지네이션
    전체를 따라가고, 그래도 안전 상한에 걸리면 그 사실을 truncated로 알려 호출부가
    "조용히 자르는" 대신 화면에 경고를 낼 수 있게 한다.
    """
    names: list[str] = []
    total_count: "int | None" = None
    truncated = False
    base_url = GITHUB_USER_INSTALLATIONS_REPOS_URL_TMPL.format(iid=installation_id)
    page = 1
    while True:
        url = f"{base_url}?{urlencode({'per_page': _INSTALLATION_REPOS_PER_PAGE, 'page': page})}"
        status, data = _http_json("GET", url, headers=_bearer_headers(user_token))
        if status >= 400:
            raise RuntimeError(
                f"GitHub installation 저장소 목록 조회에 실패했습니다 (status={status})."
            )
        if total_count is None:
            raw_total = data.get("total_count")
            if isinstance(raw_total, int):
                total_count = raw_total

        repos_raw = data.get("repositories")
        page_names = []
        if isinstance(repos_raw, list):
            for item in repos_raw:
                if isinstance(item, dict) and isinstance(item.get("full_name"), str):
                    page_names.append(item["full_name"])
        names.extend(page_names)

        if not page_names:
            # 빈 페이지 = 더 이상 없음. total_count를 못 받았거나 못 믿을 때도
            # 이 조건 하나로 루프가 반드시 끝난다(무한 루프 방어의 1차 방어선).
            break
        if total_count is not None and len(names) >= total_count:
            break
        if page >= _INSTALLATION_REPOS_MAX_PAGES:
            # 2차 방어선(안전 상한) — total_count가 비정상적으로 크거나 응답이
            # 계속 채워진 페이지만 주는 경우에도 여기서 반드시 멈춘다.
            truncated = True
            break
        page += 1
    return names, truncated


# ---------------------------------------------------------------------------
# 화면 — 최소 인라인 HTML. 외부 CSS/JS/CDN 없음. MCP 접속 주소는 표시하지 않는다
# (namu-60 대시보드 소관).
# ---------------------------------------------------------------------------
def _html_page(title: str, body_html: str) -> str:
    return (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head>"
        "<body style=\"font-family: sans-serif; max-width: 640px; margin: 40px auto; "
        f"line-height: 1.6;\">{body_html}</body></html>"
    )


def _html_connected(user_key: str, repo_full_name: str) -> str:
    body = (
        "<h1>연결 완료 (Connected)</h1>"
        f"<p>사용자 키: <code>{html.escape(user_key)}</code></p>"
        f"<p>연결된 저장소: <code>{html.escape(repo_full_name)}</code></p>"
        "<p>이제부터 이 저장소가 회원님 기억의 원본입니다. "
        "MCP 접속 주소는 별도 대시보드에서 확인하세요.</p>"
    )
    return _html_page("NAMU 연결 완료", body)


def _html_select_repo(
    user_key: str, installation_id: int, repos: "list[str]", *, truncated: bool = False
) -> str:
    items = []
    for repo in repos:
        sig = _repo_link_sig(user_key, installation_id, repo)
        qs = urlencode({"installation_id": installation_id, "repo": repo, "sig": sig})
        items.append(
            f'<li><a href="/auth/github/select-repo?{qs}">{html.escape(repo)}</a></li>'
        )
    warning = ""
    if truncated:
        # 조용한 누락이 결함의 본질이었으므로, 안전 상한에 걸려 전부 못 가져온
        # 경우는 반드시 화면에서 알린다(조용히 자르는 대안으로 바꾸지 않는다).
        warning = (
            '<p style="color:#b00020;"><strong>주의(Notice)</strong>: 저장소 목록이 너무 '
            "많아 전부 불러오지 못했습니다(안전 상한 도달) — 찾는 저장소가 아래 목록에 "
            "없다면 GitHub 설치 설정에서 허용 저장소 범위를 좁히거나 관리자에게 "
            "문의하세요. The repository list was truncated (safety limit reached); "
            "some repositories may be missing below.</p>"
        )
    body = (
        "<h1>저장소 선택 (Choose a repository)</h1>"
        f"{warning}"
        "<p>앱을 설치하며 여러 저장소를 허용했습니다. 기억을 저장할 저장소를 하나 "
        "고르세요.</p>"
        f"<ul>{''.join(items)}</ul>"
    )
    return _html_page("NAMU 저장소 선택", body)


def _html_no_repos(installation_id: int) -> str:
    settings_url = f"https://github.com/settings/installations/{installation_id}"
    body = (
        "<h1>선택된 저장소가 없습니다 (No repository selected)</h1>"
        "<p>앱 설치는 완료됐지만 접근을 허용한 저장소가 없습니다. GitHub 설치 설정에서 "
        "저장소를 하나 고르거나 새로 만들어 추가하세요.</p>"
        f'<p><a href="{html.escape(settings_url)}" target="_blank" rel="noopener">'
        "GitHub 설치 설정 열기 (Open installation settings)</a></p>"
    )
    return _html_page("NAMU 저장소 없음", body)


def _html_next_steps(user_key: str) -> str:
    # GitHub 설치 주소로 곧장 걸지 않는다 — 링크는 쿠키를 심을 수 없어 설치 후
    # 콜백이 state 없이 돌아오고, callback이 이를 거절해 400이 난다(실측). 우리
    # /auth/github/install을 한 번 경유시켜 state 쿠키를 심고 보낸다.
    install_url = "/auth/github/install"
    new_repo_url = "https://github.com/new?" + urlencode(
        {"name": "namu-memory", "visibility": "private"}
    )
    body = (
        "<h1>로그인 완료 (Logged in) — 다음 단계</h1>"
        f"<p>사용자 키: <code>{html.escape(user_key)}</code></p>"
        "<p><strong>주의</strong>: 방금 지나온 화면은 신원 확인(로그인 승인)만 했을 뿐, "
        "저장소 접근 권한은 아직 부여하지 않았습니다 — 그 화면에는 'Verify your GitHub "
        "identity', 'Know which resources you can access', 'Act on your behalf' 3줄만 "
        "떴을 것입니다. 아래 '앱 설치' 링크로 넘어가면 그때 비로소 저장소 접근 권한 "
        "목록이 표시되는 <strong>별도의 설치 승인 화면</strong>이 나옵니다 — 서로 다른 "
        "화면이니 권한 문구가 안 보였다고 놀라지 마세요.</p>"
        f'<p><a href="{html.escape(install_url)}">NAMU 앱 설치하고 저장소 연결하기 '
        "(Install the app)</a></p>"
        "<p>기억을 저장할 저장소가 아직 없다면 먼저 만드세요(비공개로 이름을 미리 "
        "채워뒀습니다):</p>"
        f'<p><a href="{html.escape(new_repo_url)}" target="_blank" rel="noopener">'
        "새 비공개 저장소 만들기 (Create a new private repo)</a></p>"
    )
    return _html_page("NAMU 다음 단계", body)


# ---------------------------------------------------------------------------
# 라우트
# ---------------------------------------------------------------------------
async def login(request: Request) -> Response:
    state = secrets.token_urlsafe(24)
    query = urlencode({"client_id": ga.client_id(), "state": state})
    resp = RedirectResponse(url=f"{GITHUB_AUTHORIZE_URL}?{query}", status_code=302)
    # redirect_uri는 의도적으로 싣지 않는다 — GitHub App에 등록된 Callback URL
    # (https://namu-cloud.onnamu.kr/auth/github/callback) 하나에만 의존한다.
    # 설정 항목을 하나 줄이는 것과 동시에, "코드의 redirect_uri와 앱 등록값이
    # 어긋나 GitHub이 거부하는" 흔한 버그 클래스 자체를 제거하기 위한 의도적
    # 선택이다.
    resp.set_cookie(
        _STATE_COOKIE_NAME,
        _sign_with_expiry(state, _STATE_COOKIE_TTL_SEC),
        max_age=_STATE_COOKIE_TTL_SEC,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
    )
    return resp


async def install(request: Request) -> Response:
    """앱 설치 화면으로 보내되, login과 똑같이 state 도장을 찍어 보낸다.

    설치를 마치면 GitHub은 등록된 Callback URL로 `code`·`installation_id`·
    `setup_action=install`을 실어 돌려보낸다. 이때 state는 **설치를 시작한
    URL에 실린 것만** 되돌아오므로, 안내 화면에서 GitHub 설치 주소를 직접
    링크하면 state 없이 돌아와 callback이 400으로 거절한다(2026-07-26 실측).
    쿠키는 링크가 아니라 응답만 심을 수 있어, 이 경로를 한 번 경유시키는 것이
    설치 왕복에 도장을 붙이는 유일한 방법이다.

    쿠키 설정은 login과 동일해야 한다 — 한쪽만 바꾸면 왕복이 조용히 깨진다.
    """
    state = secrets.token_urlsafe(24)
    query = urlencode({"state": state})
    url = f"{GITHUB_INSTALL_URL_TMPL.format(slug=_app_slug())}?{query}"
    resp = RedirectResponse(url=url, status_code=302)
    resp.set_cookie(
        _STATE_COOKIE_NAME,
        _sign_with_expiry(state, _STATE_COOKIE_TTL_SEC),
        max_age=_STATE_COOKIE_TTL_SEC,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
    )
    return resp


async def callback(request: Request) -> Response:
    query_state = request.query_params.get("state") or ""
    cookie_state = _unsign_with_expiry(request.cookies.get(_STATE_COOKIE_NAME))
    state_ok = bool(query_state) and bool(cookie_state) and hmac.compare_digest(cookie_state, query_state)
    if not state_ok:
        resp = PlainTextResponse(
            "로그인 요청을 검증하지 못했습니다(state 불일치/만료) — 처음부터 다시 "
            "로그인해 주세요. Could not verify the login request (state mismatch).",
            status_code=400,
        )
        resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
        return resp
    # state 검증 통과 — 재사용 방지를 위해 여기서 쿠키를 지운다(아래 모든 반환
    # 경로가 delete_cookie를 다시 호출하는 이유).

    code = request.query_params.get("code") or ""
    if not code:
        resp = PlainTextResponse("code 파라미터가 없습니다. Missing code.", status_code=400)
        resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
        return resp

    try:
        # user_token은 이 요청을 처리하는 동안만 쓰는 지역 변수다 — 응답을 만든
        # 뒤에는 어디에도 저장하지 않고 버려진다(설계 원칙: 영구 열쇠를 보관하지
        # 않는다).
        user_token = _exchange_code_for_token(code)
        user_info = _fetch_github_user(user_token)
    except (ValueError, RuntimeError) as exc:
        resp = PlainTextResponse(str(exc), status_code=400)
        resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
        return resp

    github_id = user_info.get("id")
    login_name = user_info.get("login")

    try:
        with closing(identity.connect()) as conn:
            user_key = identity.upsert_user(conn, github_id, login_name)

            installation_id_raw = request.query_params.get("installation_id") or ""
            if installation_id_raw:
                try:
                    installation_id = int(installation_id_raw)
                except ValueError:
                    resp = PlainTextResponse(
                        "installation_id가 올바르지 않습니다. Invalid installation_id.",
                        status_code=400,
                    )
                    resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
                    return resp

                repos, truncated = _fetch_installation_repos(user_token, installation_id)
                if truncated:
                    # 목록이 잘렸을 수 있으므로 몇 개가 잡혔든 자동 연결(1개일 때
                    # 곧장 연결하는 경로)을 타지 않는다 — "이게 정말 유일한
                    # 저장소"라는 확신이 없기 때문이다. 사용자가 직접 고르게 하고
                    # 화면에 잘렸다는 사실을 알린다.
                    body_html = _html_select_repo(
                        user_key, installation_id, repos, truncated=True
                    )
                elif len(repos) == 1:
                    identity.set_installation(conn, user_key, installation_id, repos[0])
                    body_html = _html_connected(user_key, repos[0])
                elif len(repos) == 0:
                    body_html = _html_no_repos(installation_id)
                else:
                    body_html = _html_select_repo(user_key, installation_id, repos)
            else:
                body_html = _html_next_steps(user_key)
    except (ValueError, RuntimeError) as exc:
        resp = PlainTextResponse(str(exc), status_code=400)
        resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
        return resp

    logger.info("GitHub 로그인 완료 (user_key=%s)", user_key)

    resp = HTMLResponse(body_html)
    resp.set_cookie(
        _SESSION_COOKIE_NAME,
        _sign_with_expiry(user_key, _SESSION_COOKIE_TTL_SEC),
        max_age=_SESSION_COOKIE_TTL_SEC,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/auth",
    )
    resp.delete_cookie(_STATE_COOKIE_NAME, path="/auth")
    return resp


async def select_repo(request: Request) -> Response:
    user_key = _unsign_with_expiry(request.cookies.get(_SESSION_COOKIE_NAME))
    if not user_key:
        return PlainTextResponse(
            "로그인이 필요합니다 — 세션이 없거나 만료됐습니다. "
            "Login required: session missing or expired.",
            status_code=401,
        )

    installation_id_raw = request.query_params.get("installation_id") or ""
    repo = request.query_params.get("repo") or ""
    sig = request.query_params.get("sig") or ""
    try:
        installation_id = int(installation_id_raw)
    except ValueError:
        return PlainTextResponse(
            "installation_id가 올바르지 않습니다. Invalid installation_id.", status_code=400
        )

    # repo를 그대로 믿지 않는다 — 콜백 단계에서 조회해 이 링크에 실어 보낸
    # 서명(user_key+installation_id+repo 묶음)을 재계산해 대조한다. 사용자가
    # URL을 손으로 고쳐 남의 저장소 이름을 밀어 넣어도 서명이 어긋나 통과하지
    # 못한다.
    expected_sig = _repo_link_sig(user_key, installation_id, repo)
    if not sig or not hmac.compare_digest(sig, expected_sig):
        logger.warning("select-repo 서명 검증 실패 (user_key=%s)", user_key)
        return PlainTextResponse(
            "요청이 유효하지 않습니다(서명 불일치) — 콜백 화면에서 받은 링크를 그대로 "
            "다시 이용하세요. Invalid request: signature mismatch.",
            status_code=403,
        )

    try:
        with closing(identity.connect()) as conn:
            identity.set_installation(conn, user_key, installation_id, repo)
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=400)

    return HTMLResponse(_html_connected(user_key, repo))


def build_auth_app() -> Starlette:
    """web_auth 라우트 4개를 담은 Starlette 앱(순수 ASGI callable)을 만든다.

    lifespan 훅을 선언하지 않는다 — routing_server._AuthOrMcpDispatcher가
    lifespan scope를 이 앱으로 보내지 않는다(FastMCP 세션 매니저를 기동하는
    쪽은 MCP 앱 하나뿐이어야 한다).
    """
    return Starlette(
        routes=[
            Route("/auth/github/login", login, methods=["GET"]),
            Route("/auth/github/install", install, methods=["GET"]),
            Route("/auth/github/callback", callback, methods=["GET"]),
            Route("/auth/github/select-repo", select_repo, methods=["GET"]),
        ]
    )
