"""github_app.py 유닛 테스트 — 네트워크 없이 전부 검증한다.

HTTP 접점이 `github_app._post_json` 하나로 모여 있으므로, 그 함수만
monkeypatch하면 토큰 발급·캐시·격리 로직을 전부 실측할 수 있다.

RSA 개인키는 테스트 안에서 즉석 생성한다 — 실제 GitHub App 열쇠를 저장소에
넣지 않기 위해서다(작은 키로도 RS256 서명/검증은 동일하게 검증된다).
"""
import base64
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import github_app as ga


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[str, str]:
    """(private PEM, public PEM). 세션 스코프 — 키 생성이 테스트당 반복되면 느리다."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


@pytest.fixture
def app_env(monkeypatch, rsa_keypair):
    """App ID / base64 PEM 환경변수를 세팅하고 토큰 캐시를 비운다."""
    private_pem, public_pem = rsa_keypair
    monkeypatch.setenv("NAMU_GITHUB_APP_ID", "123456")
    monkeypatch.setenv(
        "NAMU_GITHUB_APP_PRIVATE_KEY",
        base64.b64encode(private_pem.encode("utf-8")).decode("ascii"),
    )
    ga.clear_token_cache()
    yield public_pem
    ga.clear_token_cache()


class _FakePoster:
    """_post_json 대역 — 호출 횟수와 만료시각을 조종한다.

    받은 Authorization 토큰(`token`)도 함께 보관한다. 이걸 버리면 "앱 JWT로
    서명해 보낸다"가 통째로 미검증이 된다(실증됨: installation_token 안의
    app_jwt()를 빈 문자열로 바꿔도 전원 통과했다).
    """

    def __init__(self, expires_in_sec: float = 3600.0):
        self.calls: list[str] = []
        self.tokens: list[str] = []
        self.expires_in_sec = expires_in_sec
        self.counter = 0

    def __call__(self, url, token, body=None):
        self.calls.append(url)
        self.tokens.append(token)
        self.counter += 1
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.expires_in_sec)
        return {
            "token": f"ghs_faketoken_{self.counter}",
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


# ---------------------------------------------------------------------------
# 환경변수 미설정
# ---------------------------------------------------------------------------
def test_app_id_missing_raises(monkeypatch):
    monkeypatch.delenv("NAMU_GITHUB_APP_ID", raising=False)
    with pytest.raises(RuntimeError) as exc:
        ga.app_id()
    assert "NAMU_GITHUB_APP_ID" in str(exc.value)


def test_private_key_missing_raises(monkeypatch):
    monkeypatch.delenv("NAMU_GITHUB_APP_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError):
        ga.app_private_key_pem()


def test_client_id_and_secret_missing_raise(monkeypatch):
    monkeypatch.delenv("NAMU_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAMU_GITHUB_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        ga.client_id()
    with pytest.raises(RuntimeError):
        ga.client_secret()


def test_client_id_and_secret_read_lazily(monkeypatch):
    # 지연 평가 확인 — 모듈 로드 이후에 설정해도 읽힌다(2차 web_auth가 쓴다).
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_ID", "Iv1.abc")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_SECRET", "s3cr3t")
    assert ga.client_id() == "Iv1.abc"
    assert ga.client_secret() == "s3cr3t"


def test_app_jwt_missing_env_raises(monkeypatch):
    monkeypatch.delenv("NAMU_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("NAMU_GITHUB_APP_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError):
        ga.app_jwt()


# ---------------------------------------------------------------------------
# base64 PEM 복원
# ---------------------------------------------------------------------------
def test_base64_pem_is_restored(app_env, rsa_keypair):
    private_pem, _ = rsa_keypair
    assert ga.app_private_key_pem() == private_pem


def test_raw_pem_is_accepted_as_is(monkeypatch, rsa_keypair):
    # Docker secret 등이 개행 그대로 주입하는 배포 대비 관용 경로.
    private_pem, _ = rsa_keypair
    monkeypatch.setenv("NAMU_GITHUB_APP_PRIVATE_KEY", private_pem)
    assert ga.app_private_key_pem() == private_pem


def test_invalid_base64_raises_without_leaking_value(monkeypatch):
    monkeypatch.setenv("NAMU_GITHUB_APP_PRIVATE_KEY", "not-base64!!!")
    with pytest.raises(RuntimeError) as exc:
        ga.app_private_key_pem()
    assert "not-base64" not in str(exc.value)


def test_base64_of_non_pem_raises(monkeypatch):
    monkeypatch.setenv(
        "NAMU_GITHUB_APP_PRIVATE_KEY",
        base64.b64encode(b"hello world").decode("ascii"),
    )
    with pytest.raises(RuntimeError):
        ga.app_private_key_pem()


# ---------------------------------------------------------------------------
# JWT 클레임
# ---------------------------------------------------------------------------
def test_app_jwt_claims(app_env):
    public_pem = app_env
    before = datetime.now(timezone.utc).timestamp()
    token = ga.app_jwt()
    claims = jwt.decode(
        token, public_pem, algorithms=["RS256"], options={"verify_exp": False}
    )
    assert claims["iss"] == "123456"
    # 아래 60/600은 **GitHub이 정한 외부 계약값**이라 숫자 리터럴로 못 박는다 —
    # 모듈 상수(ga._JWT_*)를 참조하면 상수를 잘못 바꿨을 때 테스트가 함께
    # 끌려가 아무것도 잡지 못하는 자기참조가 된다(실증됨: 60→0, 600→1200으로
    # 바꿔도 전원 통과했다).
    # iat은 시계 오차 대비로 60초 과거로 밀려 있어야 한다(GitHub 권장).
    assert claims["iat"] <= before - 60 + 1
    # exp - iat은 GitHub 상한 10분(600초) 이내.
    assert 0 < claims["exp"] - claims["iat"] <= 600


def test_jwt_constants_match_github_contract():
    """수명 상수가 GitHub 계약값과 어긋나지 않는지 직접 못 박는다.

    exp 상한(600)만 몰래 올려두면 발급 JWT는 당장 안 바뀌어서(실제 수명이 그
    아래라) 클레임 검증으로는 안 잡힌다 — 나중에 실제 수명을 올릴 때 상한이
    이미 무력화돼 있는 함정이 되므로 여기서 값 자체를 고정한다.
    """
    assert ga._JWT_MAX_LIFETIME_SEC == 600   # GitHub: app JWT는 최대 10분
    assert ga._JWT_CLOCK_SKEW_SEC == 60      # GitHub 권장 clock skew 보정
    assert ga._JWT_LIFETIME_SEC <= 600


def test_app_jwt_uses_rs256(app_env):
    token = ga.app_jwt()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_app_jwt_accepts_injected_now(app_env):
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    claims = jwt.decode(
        ga.app_jwt(now=fixed), app_env, algorithms=["RS256"],
        options={"verify_exp": False},
    )
    # 60은 GitHub 권장 clock skew — 외부 계약값이라 리터럴로 고정한다(위 참고).
    assert claims["iat"] == int(fixed.timestamp()) - 60


# ---------------------------------------------------------------------------
# installation token 발급 + 캐시
# ---------------------------------------------------------------------------
def test_installation_token_calls_correct_url(app_env, monkeypatch):
    poster = _FakePoster()
    monkeypatch.setattr(ga, "_post_json", poster)
    token = ga.installation_token(4242)
    assert token == "ghs_faketoken_1"
    assert poster.calls == [
        "https://api.github.com/app/installations/4242/access_tokens"
    ]


def test_installation_token_authenticates_with_app_jwt(app_env, monkeypatch):
    """토큰 발급 요청이 **앱 JWT로 서명돼** 나가는지 확인한다.

    호출 URL만 보는 검증은 이 부분을 전혀 지키지 못한다 — app_jwt()를 빈
    문자열로 바꿔도 URL 단언은 그대로 통과하기 때문이다(실증됨).
    """
    public_pem = app_env
    poster = _FakePoster()
    monkeypatch.setattr(ga, "_post_json", poster)
    ga.installation_token(4242)

    assert len(poster.tokens) == 1
    sent = poster.tokens[0]
    assert sent, "Authorization에 실린 값이 비어 있다 — 앱 JWT가 붙지 않았다"
    assert jwt.get_unverified_header(sent)["alg"] == "RS256"
    # 앱 개인키로 서명됐고 iss가 설정한 App ID인지까지 확인(공개키로 검증).
    claims = jwt.decode(
        sent, public_pem, algorithms=["RS256"], options={"verify_exp": False}
    )
    assert claims["iss"] == "123456"


def test_cached_token_reused_when_fresh(app_env, monkeypatch):
    poster = _FakePoster(expires_in_sec=3600)
    monkeypatch.setattr(ga, "_post_json", poster)
    first = ga.installation_token(1)
    second = ga.installation_token(1)
    assert first == second
    assert len(poster.calls) == 1, "여유가 충분한데도 GitHub를 다시 호출했다"


def test_token_reissued_when_within_renew_margin(app_env, monkeypatch):
    # 만료까지 60초 → 갱신 여유(300초) 안쪽이므로 매번 새로 발급받아야 한다.
    poster = _FakePoster(expires_in_sec=60)
    monkeypatch.setattr(ga, "_post_json", poster)
    first = ga.installation_token(1)
    second = ga.installation_token(1)
    assert first != second
    assert len(poster.calls) == 2


def test_token_reissued_just_outside_margin_is_cached(app_env, monkeypatch):
    # 경계 바로 바깥(갱신 여유 300초 + 60초)은 재사용돼야 한다. 여기서도 300은
    # 모듈 상수가 아니라 리터럴로 둔다 — 상수를 참조하면 여유 값을 잘못 바꿔도
    # 테스트가 함께 끌려가 경계 회귀를 못 잡는다.
    poster = _FakePoster(expires_in_sec=300 + 60)
    monkeypatch.setattr(ga, "_post_json", poster)
    ga.installation_token(1)
    ga.installation_token(1)
    assert len(poster.calls) == 1


def test_cache_is_per_installation(app_env, monkeypatch):
    poster = _FakePoster(expires_in_sec=3600)
    monkeypatch.setattr(ga, "_post_json", poster)
    token_a = ga.installation_token(1)
    token_b = ga.installation_token(2)
    assert token_a != token_b, "installation이 다른데 캐시가 섞였다"
    assert len(poster.calls) == 2
    # 각자 자기 캐시를 재사용한다.
    assert ga.installation_token(1) == token_a
    assert ga.installation_token(2) == token_b
    assert len(poster.calls) == 2


def test_clear_token_cache_forces_reissue(app_env, monkeypatch):
    poster = _FakePoster(expires_in_sec=3600)
    monkeypatch.setattr(ga, "_post_json", poster)
    ga.installation_token(7)
    ga.clear_token_cache()
    ga.installation_token(7)
    assert len(poster.calls) == 2


def test_missing_token_in_response_raises(app_env, monkeypatch):
    monkeypatch.setattr(ga, "_post_json", lambda url, token, body=None: {"message": "Not Found"})
    with pytest.raises(RuntimeError) as exc:
        ga.installation_token(9)
    assert "installation_id=9" in str(exc.value)


def test_unparsable_expires_at_falls_back_to_short_life(app_env, monkeypatch):
    calls = {"n": 0}

    def poster(url, token, body=None):
        calls["n"] += 1
        return {"token": f"t{calls['n']}", "expires_at": "쓰레기값"}

    monkeypatch.setattr(ga, "_post_json", poster)
    first = ga.installation_token(3)
    # fallback 수명(3000초)은 갱신 여유보다 길므로 캐시가 유지된다 —
    # "해석 실패 = 무한 재사용"이 아니라 "보수적 수명"임을 확인.
    assert ga.installation_token(3) == first
    assert calls["n"] == 1


@pytest.mark.parametrize("bad", [0, -1, "1", None, True])
def test_invalid_installation_id_rejected(app_env, monkeypatch, bad):
    monkeypatch.setattr(ga, "_post_json", _FakePoster())
    with pytest.raises(ValueError):
        ga.installation_token(bad)


def test_token_value_not_logged(app_env, monkeypatch, caplog):
    poster = _FakePoster()
    monkeypatch.setattr(ga, "_post_json", poster)
    with caplog.at_level("DEBUG"):
        token = ga.installation_token(55)
    assert token not in caplog.text, "토큰 값이 로그에 노출됐다"


# ---------------------------------------------------------------------------
# _post_json 자체 — 여기만 httpx를 가짜로 갈아끼워 실제 구현 경로를 지난다.
# (다른 테스트는 _post_json 전체를 대역으로 바꾸므로 이 경로를 밟지 않는다.)
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def fake_httpx_post(monkeypatch):
    """httpx.post를 가로채 지정한 응답을 돌려주고, 호출 인자를 기록한다."""
    import httpx

    captured: dict = {}

    def _make(status_code: int, payload: dict):
        def _post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["json"] = json
            captured["timeout"] = timeout
            return _FakeResponse(status_code, payload)

        monkeypatch.setattr(httpx, "post", _post)
        return captured

    return _make


def test_post_json_returns_payload_and_sends_bearer(fake_httpx_post):
    captured = fake_httpx_post(200, {"token": "ghs_x", "expires_at": "2026-01-01T00:00:00Z"})
    result = ga._post_json("https://api.github.com/x", "jwt-value", body={"a": 1})
    assert result == {"token": "ghs_x", "expires_at": "2026-01-01T00:00:00Z"}
    assert captured["url"] == "https://api.github.com/x"
    assert captured["headers"]["Authorization"] == "Bearer jwt-value"
    assert captured["json"] == {"a": 1}


def test_post_json_error_does_not_leak_response_body(fake_httpx_post):
    """오류 메시지에 GitHub 응답 본문이 실리면 안 된다 — 응답에는 토큰 등
    비밀이 담길 수 있고, 예외 메시지는 로그·사용자 화면으로 흘러간다.

    구현이 status/url만 남기고 있는지 지키는 회귀 방지 단언이다(실증됨: 이
    단언이 없으면 예외에 payload를 통째로 실어도 전원 통과했다).
    """
    marker = "SENSITIVE_BODY_MARKER_9f2a"
    fake_httpx_post(403, {"message": marker, "token": marker})
    with pytest.raises(RuntimeError) as exc:
        ga._post_json("https://api.github.com/x", "jwt-value")
    message = str(exc.value)
    assert marker not in message, "예외 메시지에 응답 본문이 노출됐다"
    assert "403" in message  # 진단에 필요한 status는 남아야 한다


def test_post_json_error_does_not_leak_auth_token(fake_httpx_post):
    # Authorization에 실린 앱 JWT도 예외 메시지에 남으면 안 된다.
    fake_httpx_post(401, {"message": "Bad credentials"})
    with pytest.raises(RuntimeError) as exc:
        ga._post_json("https://api.github.com/x", "super-secret-jwt")
    assert "super-secret-jwt" not in str(exc.value)
