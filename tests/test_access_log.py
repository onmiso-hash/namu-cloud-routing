"""access_log.py 테스트 — 접속 기록에 개인 열쇠가 남지 않는지 (namu-67).

이 파일의 마지막 테스트(`test_real_server_access_log_has_no_secret`)는 유닛
테스트가 아니라 **실서버 실측**이다. 필터 함수만 검사하면 "함수는 옳은데 아무
데도 안 붙어 있다"는 형태로 결함이 되살아나도 전부 통과한다 — 이 저장소가
실제로 겪은 사고 유형이라(만들었다 ≠ 쓰인다) 진짜 uvicorn을 띄워 진짜 요청을
보내고 진짜 로그 줄을 들여다본다.
"""
from __future__ import annotations

import copy
import logging
import os
import socket
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import access_log
import identity
import routing_server as rs

SECRET = "aAwdgjg0bqcG9Na7Js1RR2sc2m9XcniJHbtPE6QK_d0"


# ---------------------------------------------------------------------------
# 지문
# ---------------------------------------------------------------------------
def test_fingerprint_is_stable_and_short():
    assert access_log.fingerprint(SECRET) == access_log.fingerprint(SECRET)
    assert len(access_log.fingerprint(SECRET)) == 8


def test_fingerprint_differs_per_secret():
    assert access_log.fingerprint(SECRET) != access_log.fingerprint(SECRET + "x")


def test_fingerprint_does_not_contain_the_secret():
    # 앞 몇 글자를 남기는 마스킹이 아니라는 것 — 원본 조각이 지문에 섞이면 안 된다.
    fp = access_log.fingerprint(SECRET)
    for length in range(4, len(SECRET)):
        assert SECRET[:length] not in fp


# ---------------------------------------------------------------------------
# 가리기
# ---------------------------------------------------------------------------
def test_mask_replaces_secret_with_fingerprint():
    line = f'127.0.0.1:40048 - "POST /mcp/{SECRET}?client=claude HTTP/1.1" 200 OK'
    masked = access_log.mask(line)
    assert SECRET not in masked
    assert f"/mcp/#{access_log.fingerprint(SECRET)}" in masked


def test_mask_keeps_diagnosis_information():
    # 완료조건 2 — 무엇이 언제 어떻게 실패했는지는 계속 보여야 한다.
    line = f'127.0.0.1:40048 - "POST /mcp/{SECRET}?client=claude HTTP/1.1" 500 ERR'
    masked = access_log.mask(line)
    for kept in ("127.0.0.1:40048", "POST", "client=claude", "HTTP/1.1", "500"):
        assert kept in masked


def test_mask_leaves_ordinary_paths_alone():
    line = '127.0.0.1 - "GET /auth/me HTTP/1.1" 200 OK'
    assert access_log.mask(line) == line


def test_mask_leaves_bare_mcp_path_alone():
    # 열쇠 없는 고정 마운트 경로 자체는 비밀이 아니다.
    line = '127.0.0.1 - "POST /mcp HTTP/1.1" 200 OK'
    assert access_log.mask(line) == line


def test_mask_is_idempotent():
    line = f"POST /mcp/{SECRET}"
    once = access_log.mask(line)
    assert access_log.mask(once) == once


def test_mask_hides_login_round_trip_credentials():
    line = '"GET /auth/github/callback?code=abc123secret&state=xyz789 HTTP/1.1" 302'
    masked = access_log.mask(line)
    assert "abc123secret" not in masked
    assert "xyz789" not in masked
    assert "/auth/github/callback" in masked


# ---------------------------------------------------------------------------
# 필터
# ---------------------------------------------------------------------------
def _uvicorn_style_record() -> logging.LogRecord:
    """uvicorn.access가 실제로 만드는 모양의 레코드(경로는 args에 들어 있다)."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:40048",
            "POST",
            f"/mcp/{SECRET}?client=claude",
            "1.1",
            200,
        ),
        exc_info=None,
    )


def test_filter_masks_secret_in_record_args():
    record = _uvicorn_style_record()
    assert access_log.SecretMaskingFilter().filter(record) is True
    assert SECRET not in record.getMessage()
    assert access_log.fingerprint(SECRET) in record.getMessage()


def test_filter_keeps_non_string_args_intact():
    # AccessFormatter가 args를 5개로 풀어 status_code를 int로 쓴다 — 모양이 깨지면
    # 로그가 아예 못 찍힌다.
    record = _uvicorn_style_record()
    access_log.SecretMaskingFilter().filter(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5
    assert record.args[4] == 200


def test_filter_masks_secret_in_message_text():
    record = logging.LogRecord(
        name="whatever", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"뭔가 잘못됨: /mcp/{SECRET}", args=None, exc_info=None,
    )
    access_log.SecretMaskingFilter().filter(record)
    assert SECRET not in record.getMessage()


# ---------------------------------------------------------------------------
# 설정 조립
# ---------------------------------------------------------------------------
def test_build_log_config_attaches_filter_to_loggers_and_handlers():
    config = access_log.build_log_config()
    filter_names = set(config["filters"])
    assert filter_names
    name = next(iter(filter_names))
    for section in ("loggers", "handlers"):
        for entry in config[section].values():
            assert name in entry["filters"]


def test_build_log_config_does_not_mutate_the_base():
    import uvicorn.config

    before = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    access_log.build_log_config()
    assert uvicorn.config.LOGGING_CONFIG == before


def test_build_log_config_refuses_unknown_shape():
    # uvicorn이 기본 설정 모양을 바꾸면 필터가 아무 데도 안 붙은 채 서버가 뜬다.
    # 그건 증상 없이 열쇠가 다시 새기 시작하는 것이므로 기동을 멈춰야 한다.
    with pytest.raises(RuntimeError):
        access_log.build_log_config({"version": 1, "loggers": {}, "handlers": {}})


def test_main_actually_hands_the_config_to_uvicorn(monkeypatch, tmp_path):
    """만들었다 ≠ 쓰인다 — main()이 실제로 log_config를 넘기는지 본다."""
    import uvicorn

    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(rs, "build_app", lambda: object())
    monkeypatch.setenv("NAMU_HTTP_PORT", "8770")
    rs.main()

    config = captured.get("log_config")
    assert config is not None, "main()이 log_config 없이 서버를 띄운다"
    name = next(iter(config["filters"]))
    assert name in config["loggers"]["uvicorn.access"]["filters"]


# ---------------------------------------------------------------------------
# 실서버 실측
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_server_access_log_has_no_secret(tmp_path):
    """진짜 서버를 띄워 진짜 요청을 보내고, 남은 접속 기록을 눈으로 확인한다.

    별도 프로세스로 도는 이유는 helper_access_log_server.py 문서에 적혀 있다.
    """
    db_path = tmp_path / "identity.db"
    env = dict(os.environ)
    env.update(
        NAMU_IDENTITY_DB_PATH=str(db_path),
        NAMU_STORE_ROOT=str(tmp_path / "store"),
        NAMU_USER_ROOT=str(tmp_path / "users"),
        PYTHONUNBUFFERED="1",
    )
    env.pop("NAMU_HTTP_TOKEN", None)

    with closing(identity.connect(db_path)) as conn:
        identity.init_db(conn)
        user_key = identity.upsert_user(conn, 4242, "tester")
        secret = identity.get_by_user_key(conn, user_key)["mcp_secret"]

    helper = Path(__file__).with_name("helper_access_log_server.py")
    result = subprocess.run(
        [sys.executable, str(helper), str(_free_port()), secret],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    logged = result.stdout + result.stderr
    assert result.returncode == 0, f"시험용 서버가 실패했다:\n{logged}"

    assert "/mcp/" in logged, f"access 로그 줄 자체가 안 잡혔다:\n{logged}"
    assert secret not in logged, f"접속 기록에 개인 열쇠가 그대로 남았다:\n{logged}"
    assert f"/mcp/#{access_log.fingerprint(secret)}" in logged
    assert "200" in logged
