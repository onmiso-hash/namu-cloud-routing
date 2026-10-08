"""캐릭터 도구(나무 캐릭터 1단계) — 회원 폴더로 갈라 쓰기, 공개 저장소 거절, 올리기.

저장·검사 로직 자체는 코어(vendor/namu-agent/namu-plugin/test_character.py)가 검사한다.
여기서는 이 서버가 더하는 몫만 본다.
"""
import pytest

import attach_files
import github_app
import routing_server as rs
import user_repo as ur


class _FakeRequest:
    def __init__(self, query_params: dict):
        self.query_params = query_params


class _FakeRequestContext:
    def __init__(self, request):
        self.request = request


class _FakeCtx:
    def __init__(self, user: str):
        self.request_context = _FakeRequestContext(
            _FakeRequest({"user": user, "client": "claude"})
        )


def _card(name="하린", **over):
    card = {
        "id": None, "name": name, "aliases": [], "personality": ["다정하고 차분함"],
        "speech": "반말", "emoji": "가끔 써요", "call_user": "허니",
        "relationship_start": "stranger", "relationship_ceiling": "lover",
        "likes": [], "sample_lines": [], "expression_level": None,
    }
    card.update(over)
    return card


@pytest.fixture(autouse=True)
def _connected_member(monkeypatch, tmp_path):
    """로그인하고 비공개 저장소를 연결한 회원처럼 동작하게 하는 대역."""
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))

    def _stub_ensure_ready(conn, key):
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    pushes = []
    monkeypatch.setattr(ur, "ensure_ready", _stub_ensure_ready)
    monkeypatch.setattr(
        ur, "push",
        lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: pushes.append(key) or True,
    )
    monkeypatch.setattr(
        attach_files, "_repo_and_token", lambda conn, key: (f"{key}/namu-memory", "tok")
    )
    privacy = {"private": True, "calls": 0}

    def _is_private(repo, token):
        privacy["calls"] += 1
        if isinstance(privacy["private"], Exception):
            raise privacy["private"]
        return privacy["private"]

    monkeypatch.setattr(github_app, "repo_is_private", _is_private)
    monkeypatch.setattr(rs, "_private_repo_checked", {})
    return {"pushes": pushes, "privacy": privacy}


def test_save_list_load_round_trip(_connected_member, tmp_path):
    r = rs.namu_character_save(_card(aliases=["린아"]), ctx=_FakeCtx("alice"))
    assert r["created"] is True
    assert _connected_member["pushes"] == ["alice"]
    card_dir = tmp_path / "users" / "alice" / "memory" / "character" / r["id"] / "card"
    assert (card_dir / f"{r['version']}.yaml").is_file()

    listed = rs.namu_character_list(ctx=_FakeCtx("alice"))["characters"]
    assert [c["name"] for c in listed] == ["하린"]
    loaded = rs.namu_character_load("린아", ctx=_FakeCtx("alice"))
    assert loaded["id"] == r["id"]
    assert '너는 지금부터 "하린"이다.' in loaded["persona"]


def test_members_are_kept_apart():
    rs.namu_character_save(_card(), ctx=_FakeCtx("alice"))
    assert rs.namu_character_list(ctx=_FakeCtx("bob"))["characters"] == []
    with pytest.raises(ValueError):
        rs.namu_character_load("하린", ctx=_FakeCtx("bob"))
    # 다른 회원이면 같은 이름도 된다.
    rs.namu_character_save(_card(), ctx=_FakeCtx("bob"))


def test_public_repository_is_refused(_connected_member, tmp_path):
    _connected_member["privacy"]["private"] = False
    with pytest.raises(ValueError, match="공개 저장소"):
        rs.namu_character_save(_card(), ctx=_FakeCtx("alice"))
    assert not (tmp_path / "users" / "alice" / "memory" / "character").exists()
    assert _connected_member["pushes"] == []


def test_unknown_privacy_is_refused(_connected_member):
    _connected_member["privacy"]["private"] = RuntimeError("GitHub 503")
    with pytest.raises(ValueError, match="확인하지 못해"):
        rs.namu_character_save(_card(), ctx=_FakeCtx("alice"))


def test_private_result_is_remembered_briefly(_connected_member):
    rs.namu_character_save(_card("하린"), ctx=_FakeCtx("alice"))
    rs.namu_character_save(_card("도윤"), ctx=_FakeCtx("alice"))
    assert _connected_member["privacy"]["calls"] == 1


def test_public_result_is_not_remembered(_connected_member):
    # 공개였다가 비공개로 바꾼 회원이 바로 쓸 수 있어야 한다.
    _connected_member["privacy"]["private"] = False
    with pytest.raises(ValueError):
        rs.namu_character_save(_card(), ctx=_FakeCtx("alice"))
    _connected_member["privacy"]["private"] = True
    assert rs.namu_character_save(_card(), ctx=_FakeCtx("alice"))["created"] is True


def test_reading_does_not_need_the_privacy_check(_connected_member):
    rs.namu_character_list(ctx=_FakeCtx("alice"))
    rs.namu_character_schema(ctx=_FakeCtx("alice"))
    assert _connected_member["privacy"]["calls"] == 0


def test_push_failure_keeps_the_save_and_warns(monkeypatch):
    def _fail(conn, key, message=ur.DEFAULT_COMMIT_MESSAGE):
        raise ur.UserRepoError("push rejected")

    monkeypatch.setattr(ur, "push", _fail)
    r = rs.namu_character_save(_card(), ctx=_FakeCtx("alice"))
    assert r["created"] is True and "warning" in r
    assert rs.namu_character_load("하린", ctx=_FakeCtx("alice"))["id"] == r["id"]


def test_character_tools_are_exposed():
    assert {
        "namu_character_list", "namu_character_schema",
        "namu_character_save", "namu_character_load",
    } <= rs.EXPOSED_TOOLS
