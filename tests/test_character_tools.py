"""캐릭터 도구(나무 캐릭터 1·2단계) — 회원 폴더로 갈라 쓰기, 공개 저장소 거절, 올리기.

저장·검사 로직 자체는 코어(vendor/namu-agent/namu-plugin/test_character.py)가 검사한다.
여기서는 이 서버가 더하는 몫만 본다.
"""
import pytest

from contextlib import closing

import github_app
import identity
import routing_server as rs
import user_repo as ur
import web_auth


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


ALICE, BOB = "gh-1", "gh-2"


@pytest.fixture(autouse=True)
def _connected_member(monkeypatch, tmp_path):
    """로그인하고 저장소를 연결한 회원 둘(장부에 실제로 적는다) + GitHub 대역."""
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))
    with closing(identity.connect()) as conn:
        for github_id, login in ((1, "alice"), (2, "bob")):
            key = identity.upsert_user(conn, github_id, login)
            identity.set_installation(conn, key, 100 + github_id, f"{login}/namu-memory")

    def _stub_ensure_ready(conn, key):
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    pushes = []
    monkeypatch.setattr(ur, "ensure_ready", _stub_ensure_ready)
    monkeypatch.setattr(
        ur, "push",
        lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: pushes.append(key) or True,
    )
    monkeypatch.setattr(github_app, "installation_token", lambda iid: "tok")
    privacy = {"private": True, "calls": 0}

    def _is_private(repo, token):
        privacy["calls"] += 1
        if isinstance(privacy["private"], Exception):
            raise privacy["private"]
        return privacy["private"]

    monkeypatch.setattr(github_app, "repo_is_private", _is_private)
    return {"pushes": pushes, "privacy": privacy}


def _stored(key):
    with closing(identity.connect()) as conn:
        return identity.get_repo_private(conn, key)


def _relogin(key):
    """다시 로그인했을 때 콜백이 하는 확인과 같은 함수를 부른다."""
    with closing(identity.connect()) as conn:
        web_auth._check_repo_privacy(conn, key)


def test_save_list_load_round_trip(_connected_member, tmp_path):
    r = rs.namu_character_save(_card(aliases=["린아"]), ctx=_FakeCtx(ALICE))
    assert r["created"] is True
    assert _connected_member["pushes"] == [ALICE]
    card_dir = tmp_path / "users" / ALICE / "memory" / "character" / r["id"] / "card"
    assert (card_dir / f"{r['version']}.yaml").is_file()

    listed = rs.namu_character_list(ctx=_FakeCtx(ALICE))["characters"]
    assert [c["name"] for c in listed] == ["하린"]
    loaded = rs.namu_character_load("린아", ctx=_FakeCtx(ALICE))
    assert loaded["id"] == r["id"]
    assert '너는 지금부터 "하린"이다.' in loaded["persona"]


def test_members_are_kept_apart():
    rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    assert rs.namu_character_list(ctx=_FakeCtx(BOB))["characters"] == []
    with pytest.raises(ValueError):
        rs.namu_character_load("하린", ctx=_FakeCtx(BOB))
    # 다른 회원이면 같은 이름도 된다.
    rs.namu_character_save(_card(), ctx=_FakeCtx(BOB))


# ── 비공개 확인: 한 번 묻고 그 답을 계속 쓴다, 다시 로그인하면 새로 묻는다 ──────
def test_login_asks_once_and_saves_reuse_the_answer(_connected_member):
    _relogin(ALICE)
    assert _stored(ALICE) is True and _connected_member["privacy"]["calls"] == 1
    rs.namu_character_save(_card("하린"), ctx=_FakeCtx(ALICE))
    rs.namu_character_save(_card("도윤"), ctx=_FakeCtx(ALICE))
    assert _connected_member["privacy"]["calls"] == 1


def test_member_connected_before_this_feature_is_asked_on_first_save(_connected_member):
    assert _stored(ALICE) is None
    rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    assert _stored(ALICE) is True and _connected_member["privacy"]["calls"] == 1


def test_public_repository_is_refused(_connected_member, tmp_path):
    _connected_member["privacy"]["private"] = False
    with pytest.raises(ValueError, match="공개 저장소"):
        rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    assert not (tmp_path / "users" / ALICE / "memory" / "character").exists()
    assert _connected_member["pushes"] == []


def test_making_it_private_takes_effect_at_the_next_login(_connected_member):
    _connected_member["privacy"]["private"] = False
    _relogin(ALICE)
    _connected_member["privacy"]["private"] = True  # GitHub에서 비공개로 바꿨다
    with pytest.raises(ValueError, match="다시 로그인"):
        rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    _relogin(ALICE)
    assert rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))["created"] is True


def test_unknown_privacy_is_refused_and_asked_again(_connected_member):
    _connected_member["privacy"]["private"] = RuntimeError("GitHub 503")
    with pytest.raises(ValueError, match="확인하지 못해"):
        rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    assert _stored(ALICE) is None
    _connected_member["privacy"]["private"] = True
    assert rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))["created"] is True


def test_login_is_not_broken_when_github_cannot_answer(_connected_member):
    _connected_member["privacy"]["private"] = RuntimeError("GitHub 503")
    _relogin(ALICE)  # 예외가 올라오지 않아야 한다
    assert _stored(ALICE) is None


def test_connecting_another_repository_forgets_the_old_answer(_connected_member):
    _relogin(ALICE)
    with closing(identity.connect()) as conn:
        identity.set_installation(conn, ALICE, 101, "alice/other-repo")
    assert _stored(ALICE) is None


def test_reading_does_not_need_the_privacy_check(_connected_member):
    rs.namu_character_list(ctx=_FakeCtx(ALICE))
    rs.namu_character_schema(ctx=_FakeCtx(ALICE))
    assert _connected_member["privacy"]["calls"] == 0


def test_push_failure_keeps_the_save_and_warns(monkeypatch):
    def _fail(conn, key, message=ur.DEFAULT_COMMIT_MESSAGE):
        raise ur.UserRepoError("push rejected")

    monkeypatch.setattr(ur, "push", _fail)
    r = rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    assert r["created"] is True and "warning" in r
    assert rs.namu_character_load("하린", ctx=_FakeCtx(ALICE))["id"] == r["id"]


def test_character_tools_are_exposed():
    assert {
        "namu_character_list", "namu_character_schema",
        "namu_character_save", "namu_character_load",
        "namu_character_diary", "namu_character_core",
    } <= rs.EXPOSED_TOOLS


# ── 2단계: 일기·핵심 기억 ────────────────────────────────────────────────────
def test_diary_and_core_round_trip(_connected_member, tmp_path):
    saved = rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    out = rs.namu_character_diary(
        "하린", "처음 같이 산책했다.", 9, "즐거움", core_candidates=["첫 산책은 한강"],
        ctx=_FakeCtx(ALICE),
    )
    assert out["affection_delta"] == 5 and out["clipped_from"] == 9
    char_dir = tmp_path / "users" / ALICE / "memory" / "character" / saved["id"]
    assert (char_dir / "diary" / f"{out['id']}.yaml").is_file()
    pid = out["pending_added"][0]["id"]

    listed = rs.namu_character_core("하린", ctx=_FakeCtx(ALICE))
    assert listed["pending"] == [{"id": pid, "text": "첫 산책은 한강"}]
    res = rs.namu_character_core("하린", "confirm", [pid], ctx=_FakeCtx(ALICE))
    assert res["confirmed"][0]["id"] == pid
    assert (char_dir / "core" / f"{pid}.yaml").is_file()
    loaded = rs.namu_character_load("하린", ctx=_FakeCtx(ALICE))
    assert loaded["relationship"]["affection"] == 15
    assert loaded["core_memories"] == [{"id": pid, "text": "첫 산책은 한강"}]
    # 저장·일기·확정 세 번 모두 올렸다.
    assert _connected_member["pushes"] == [ALICE] * 3


def test_diary_and_core_writes_are_refused_on_a_public_repository(_connected_member, tmp_path):
    rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    out = rs.namu_character_diary("하린", "수다.", core_candidates=["기억"], ctx=_FakeCtx(ALICE))
    pid = out["pending_added"][0]["id"]
    with closing(identity.connect()) as conn:
        identity.set_repo_private(conn, ALICE, False)
    with pytest.raises(ValueError, match="공개 저장소"):
        rs.namu_character_diary("하린", "수다.", ctx=_FakeCtx(ALICE))
    with pytest.raises(ValueError, match="공개 저장소"):
        rs.namu_character_core("하린", "confirm", [pid], ctx=_FakeCtx(ALICE))
    # 보기만 하는 것은 된다.
    assert rs.namu_character_core("하린", ctx=_FakeCtx(ALICE))["pending"][0]["id"] == pid


def test_diaries_are_kept_per_member():
    rs.namu_character_save(_card(), ctx=_FakeCtx(ALICE))
    rs.namu_character_save(_card(), ctx=_FakeCtx(BOB))
    rs.namu_character_diary("하린", "앨리스와.", 5, "즐거움", ctx=_FakeCtx(ALICE))
    assert rs.namu_character_load("하린", ctx=_FakeCtx(ALICE))["relationship"]["affection"] == 15
    assert rs.namu_character_load("하린", ctx=_FakeCtx(BOB))["relationship"]["affection"] == 10
