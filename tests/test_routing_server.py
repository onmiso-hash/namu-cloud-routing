"""routing_server.py 유닛 테스트 — 가짜 ctx(query_params 지정)로 tool을 직접
호출하는 패턴(vendor/namu-agent의 test_mcp_via.py 참고).

routing_server는 import 시점에 실제 데이터를 건드리지 않으므로(코어 모듈
config/db/profile 자체가 import 시 side-effect 없음 — mcp_server.py의
`_ensure_db()` 같은 부팅 로직은 미러링하지 않았다), in-process import로 충분하다.
매 테스트는 `NAMU_STORE_ROOT`를 tmp_path 하위로 monkeypatch해 STORE_ROOT를 격리한다.
"""
import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import identity
import routing_server as rs
import task_resolve
import user_repo as ur

# 오토유즈 스텁(_identity_and_repo_sync_stub)이 ur.ensure_ready/ur.push를 대역으로
# 바꾸기 전에 실제 함수를 붙잡아 둔다 — "미연결 사용자 거부"처럼 user_repo의
# 진짜 동작(RepoNotConnected 판정)을 겨냥하는 테스트가 이 참조로 되돌릴 수 있다.
_REAL_ENSURE_READY = ur.ensure_ready
_REAL_PUSH = ur.push


class _FakeUrl:
    scheme = "https"
    netloc = "namu-cloud.example"


class _FakeRequest:
    def __init__(self, query_params: dict):
        self.query_params = query_params
        # 티켓 도구는 링크를 지으려고 요청에서 바깥 주소를 끌어낸다
        # (web_auth._public_origin) — 그 두 칸만 흉내 낸다.
        self.headers = {"host": "namu-cloud.example"}
        self.url = _FakeUrl()


class _FakeRequestContext:
    def __init__(self, request):
        self.request = request


class _FakeCtx:
    def __init__(self, query_params: dict):
        self.request_context = _FakeRequestContext(_FakeRequest(query_params))


def _ctx(user: str | None = None, client: str | None = "claude") -> _FakeCtx:
    # 공용 서버는 개인용을 미러해 ?user=(라우팅)와 ?client=(출처/via)를 함께 받는다.
    # 정상 경로 테스트가 항상 유효한 client를 싣도록 기본값 "claude"를 준다.
    params: dict = {}
    if user is not None:
        params["user"] = user
    if client is not None:
        params["client"] = client
    return _FakeCtx(params)


@pytest.fixture(autouse=True)
def _store_root(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _identity_and_repo_sync_stub(monkeypatch, tmp_path):
    """namu-58 4차 배선 이후 3도구는 모두 `identity.connect()` +
    `user_repo.ensure_ready`/`push`를 거친다 — 이 파일의 절대다수 테스트는
    저장소 동기화 자체가 관심사가 아니라(격리·키 검증·kind 라우팅 등 순수
    라우팅 로직 검증) "이미 로그인하고 저장소를 연결한 사용자"처럼 동작하게
    만드는 무해한 대역이 필요하다.

    - `NAMU_IDENTITY_DB_PATH`: identity.connect()가 요구하는 필수 환경변수.
      파일 기반으로 둬야 한 테스트 안의 여러 tool 호출(예: record 후 recall)이
      같은 장부를 공유한다(`:memory:`는 호출마다 새 커넥션이라 공유가 안 된다).
    - `user_repo.ensure_ready`: 진짜로 실행하면 GitHub 네트워크(github_app 토큰
      발급, git clone)가 필요하므로, 그 대신 "이미 clone된 사본이 있다"만
      흉내낸다(`.git` 폴더만 만든다) — `RepoNotConnected` 같은 진짜 연결 관문은
      전혀 검사하지 않는다.
    - `user_repo.push`: 대부분의 테스트가 push 성패에 관심이 없으므로 아무 일도
      하지 않고 "변경 없음"(False)을 돌려준다.

    이 스텁 자체(TTL 판정·미연결 거부·push 호출·push 실패 시 경고)를 검증하는
    아래 전용 테스트들은 각자 필요한 부분만 다시 monkeypatch해 실제 동작이나
    별도의 스파이(spy)로 되돌린다.
    """
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))

    def _stub_ensure_ready(conn, key):
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ur, "ensure_ready", _stub_ensure_ready)
    monkeypatch.setattr(
        ur, "push", lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: False
    )


# ---------------------------------------------------------------------------
# 왕복: record → recall (같은 사용자)
# ---------------------------------------------------------------------------
def test_record_then_recall_round_trip(tmp_path):
    entry_id = rs.namu_record(
        bowl="learnings", topic="구현 작업", summary="구현을 마쳤다",
        reason="테스트라 성공", body="경위 전문", status="success",
        ctx=_ctx("alice"),
    )
    assert isinstance(entry_id, str) and entry_id

    result = rs.namu_recall(ctx=_ctx("alice"))
    assert "profile" in result and "learnings" in result
    ids = [d["id"] for d in result["learnings"]]
    assert entry_id in ids

    # 실제로 users/alice/ 아래에 물리적으로 남았는지 확인
    yaml_path = tmp_path / "users" / "alice" / "memory" / "learnings.yaml"
    db_path = tmp_path / "users" / "alice" / "db" / "namu.db"
    assert yaml_path.exists()
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM learnings WHERE id = ?", (entry_id,)
        ).fetchone()
    assert row == (entry_id,)


def test_search_finds_recorded_entry():
    rs.namu_record(
        bowl="learnings", topic="검색용 작업", summary="검색으로 찾을 항목",
        reason="search로 찾을 이유", body="생략", status="success",
        ctx=_ctx("alice"),
    )
    result = rs.namu_search("검색용", ctx=_ctx("alice"))
    assert result["results"], "search가 방금 기록한 항목을 찾지 못함"
    assert any("검색용" in r["task"] for r in result["results"])


# ---------------------------------------------------------------------------
# 두 사용자 완전 격리
# ---------------------------------------------------------------------------
def test_two_users_fully_isolated(tmp_path):
    id_alice = rs.namu_record(
        bowl="learnings", topic="alice 작업", summary="alice 요약",
        reason="alice 이유", body="생략", status="success",
        ctx=_ctx("alice"),
    )
    id_bob = rs.namu_record(
        bowl="learnings", topic="bob 작업", summary="bob 요약",
        reason="bob 이유", body="생략", status="failure",
        ctx=_ctx("bob"),
    )

    recall_alice = rs.namu_recall(ctx=_ctx("alice"))
    recall_bob = rs.namu_recall(ctx=_ctx("bob"))

    alice_ids = {d["id"] for d in recall_alice["learnings"]}
    bob_ids = {d["id"] for d in recall_bob["learnings"]}

    assert id_alice in alice_ids
    assert id_alice not in bob_ids
    assert id_bob in bob_ids
    assert id_bob not in alice_ids

    # 물리 경로도 서로 다르고 서로 침범 안 함
    alice_yaml = tmp_path / "users" / "alice" / "memory" / "learnings.yaml"
    bob_yaml = tmp_path / "users" / "bob" / "memory" / "learnings.yaml"
    assert alice_yaml.exists() and bob_yaml.exists()
    assert "bob 작업" not in alice_yaml.read_text(encoding="utf-8")
    assert "alice 작업" not in bob_yaml.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 키 없음/빈 값 거부
# ---------------------------------------------------------------------------
def test_missing_user_key_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx(None))


def test_empty_user_key_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx(""))


def test_no_ctx_at_all_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=None)


def test_record_missing_user_key_rejected(tmp_path):
    with pytest.raises(ValueError):
        rs.namu_record(
            task="t", outcome="success", reason="r", ctx=_ctx(None),
        )
    # STORE_ROOT/users 자체가 생기지 않았어야 함
    assert not (tmp_path / "users").exists()


# ---------------------------------------------------------------------------
# 출처(client/via) — 개인용 미러: ?client= 없거나 형식 틀리면 거부, 있으면 저장
# ---------------------------------------------------------------------------
def test_missing_client_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx("alice", client=None))


def test_invalid_client_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx("alice", client="bad name!"))


def test_record_missing_client_rejected(tmp_path):
    with pytest.raises(ValueError):
        rs.namu_record(
            task="t", outcome="success", reason="r",
            ctx=_ctx("alice", client=None),
        )


def test_via_stored_on_record(tmp_path):
    # ?client=gemini 로 기록하면 그 항목의 via 컬럼에 'gemini'가 저장돼야 한다.
    # 이 검증이 없으면 공용 서버가 AI 출처를 흘리는 회귀(개인용 미러 누락)가 재발한다.
    entry_id = rs.namu_record(
        bowl="learnings", topic="출처 저장 작업", summary="출처 저장 확인",
        reason="via 저장 확인", body="생략", status="success",
        ctx=_ctx("alice", client="gemini"),
    )
    db_path = tmp_path / "users" / "alice" / "db" / "namu.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT via FROM learnings WHERE id = ?", (entry_id,)
        ).fetchone()
    assert row == ("gemini",)


# ---------------------------------------------------------------------------
# 불안전 키 거부 — 경로 이탈 방지 (핵심 보안 경계)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad_key",
    [
        "../etc",
        "a/b",
        "/etc/passwd",
        "..",
        "a\\b",
        "a b",  # 공백도 허용 슬러그 밖
        "a" * 100,  # 길이 초과
    ],
)
def test_unsafe_user_key_rejected(tmp_path, bad_key):
    with pytest.raises(ValueError):
        rs.namu_record(
            task="t", outcome="success", reason="r", ctx=_ctx(bad_key),
        )
    # STORE_ROOT/users 밖에 아무 파일도 생기지 않았어야 함
    users_root = tmp_path / "users"
    if users_root.exists():
        for path in users_root.rglob("*"):
            resolved = path.resolve()
            assert str(resolved).startswith(str(users_root.resolve()))
    # STORE_ROOT 밖(예: 상위 디렉토리)에도 새 파일이 생기지 않았어야 함
    assert not (tmp_path.parent / "etc").exists()


def test_unsafe_user_key_recall_also_rejected():
    with pytest.raises(ValueError):
        rs.namu_recall(ctx=_ctx("../escape"))
    with pytest.raises(ValueError):
        rs.namu_search("q", ctx=_ctx("../escape"))


# ---------------------------------------------------------------------------
# bowl='profile' → profile.yaml 라우팅, namu_recall 그릇별 반환
# ---------------------------------------------------------------------------
def test_profile_bowl_routes_to_profile_yaml(tmp_path):
    fact_id = rs.namu_record(
        bowl="profile",
        topic="alice",
        summary="한국어 선호",
        reason="본인 발화",
        body="생략",
        ctx=_ctx("alice"),
    )
    assert isinstance(fact_id, str) and fact_id

    profile_path = tmp_path / "users" / "alice" / "memory" / "profile.yaml"
    assert profile_path.exists()
    assert "한국어 선호" in profile_path.read_text(encoding="utf-8")

    # 같은 사용자의 learnings.yaml에는 안 들어감(다른 그릇)
    yaml_path = tmp_path / "users" / "alice" / "memory" / "learnings.yaml"
    if yaml_path.exists():
        assert "한국어 선호" not in yaml_path.read_text(encoding="utf-8")

    result = rs.namu_recall(ctx=_ctx("alice"))
    assert "profile" in result and "learnings" in result
    profile_ids = [d["id"] for d in result["profile"]]
    assert fact_id in profile_ids


def test_profile_bowl_missing_reason_rejected():
    with pytest.raises(ValueError):
        rs.namu_record(
            bowl="profile", topic="alice", summary="stmt", reason="", body="b",
            ctx=_ctx("alice"),
        )


# ---------------------------------------------------------------------------
# 3층 저장(namu-68) — 클라우드가 개인용과 같은 형태로 남기는가.
#
# 이 절이 지키는 것: 2026-07-31 실측에서 클라우드로 남긴 기억에 summary와 body가
# 아예 없었다. 겉(라우팅 서버)만 최신이고 속(vendor 코어)이 07-18자에 멈춰 있었기
# 때문인데, 그 상태에서도 기존 테스트는 전부 통과했다 — 옛 코어에는 3층이라는
# 개념 자체가 없어 아무도 그 부재를 묻지 않았기 때문이다. 아래 테스트들이 그
# 침묵을 깬다: **파일에 실제로 무엇이 적혔는지**를 본다.
# ---------------------------------------------------------------------------
def _yaml_text(tmp_path, user: str, name: str) -> str:
    return (tmp_path / "users" / user / "memory" / name).read_text(encoding="utf-8")


def test_learnings_record_stores_all_three_layers(tmp_path):
    rs.namu_record(
        bowl="learnings", topic="3층 확인", summary="요약 한 줄",
        reason="왜 그런가", body="그때 무슨 일이 있었나 — 원문 전문",
        status="success", ctx=_ctx("alice"),
    )
    text = _yaml_text(tmp_path, "alice", "learnings.yaml")
    assert "요약 한 줄" in text
    assert "왜 그런가" in text
    assert "그때 무슨 일이 있었나 — 원문 전문" in text


def test_profile_record_stores_all_three_layers(tmp_path):
    rs.namu_record(
        bowl="profile", topic="alice", summary="한국어를 쓴다",
        reason="본인이 그렇게 말했다", body="대화 원문 전문",
        ctx=_ctx("alice"),
    )
    text = _yaml_text(tmp_path, "alice", "profile.yaml")
    assert "한국어를 쓴다" in text
    assert "본인이 그렇게 말했다" in text
    assert "대화 원문 전문" in text


def test_memo_bowl_is_stored_and_comes_back_in_recall(tmp_path):
    """쪽지는 07-28에 조사 원문을 통째로 잃은 그릇이다 — 원문(body)이 파일에
    남고, 웹에는 세션 훅이 없으므로 recall 반환으로 다시 떠야 한다."""
    memo_id = rs.namu_record(
        bowl="memo", summary="이커머스 조사 자료", reason="나중에 쓰려고",
        body="조사 자료 원문 전문", ctx=_ctx("alice"),
    )
    text = _yaml_text(tmp_path, "alice", "memo.yaml")
    assert "조사 자료 원문 전문" in text

    result = rs.namu_recall(ctx=_ctx("alice"))
    assert [m["id"] for m in result["memo"]] == [memo_id]
    # 다른 그릇에 섞이지 않는다.
    assert not result["learnings"]


def test_attachments_bowl_is_stored_in_its_own_file(tmp_path):
    """첨부 기록은 쪽지로 새면 안 된다 — 예전 이 자리의 분기가 `else: # memo`라,
    그릇이 하나 늘면 새 그릇이 말없이 쪽지로 저장되는 구조였다."""
    entry_id = rs.namu_record(
        bowl="attachments", path="attach_file/설계.md", bytes=284915,
        status="올림", summary="설계 문서", reason="파일째 남긴다",
        body="원문", topic="namu-70", project="proj-x", ctx=_ctx("alice"),
    )
    text = _yaml_text(tmp_path, "alice", "attachments.yaml")
    assert "attach_file/설계.md" in text
    assert "284915" in text
    # 쪽지 파일은 생기지도 않는다.
    assert not (tmp_path / "users" / "alice" / "memory" / "memo.yaml").exists()

    found = rs.namu_search(bowl="attachments", ctx=_ctx("alice"))
    assert found["count"] == 1
    assert found["results"][0]["id"] == entry_id
    assert found["results"][0]["bytes"] == 284915


def test_attachments_search_filters_by_project(tmp_path):
    for name, project in (("a.pdf", "proj-x"), ("b.pdf", "다른방")):
        rs.namu_record(
            bowl="attachments", path=f"attach_file/{name}", bytes=10,
            status="올림", summary="s", reason="r", body="b",
            project=project, ctx=_ctx("alice"),
        )
    found = rs.namu_search(bowl="attachments", project="proj-x", ctx=_ctx("alice"))
    assert [e["path"] for e in found["results"]] == ["attach_file/a.pdf"]


def test_attachments_require_a_size(tmp_path):
    """크기가 비면 거절한다 — 비어 있으면 목록 도구가 크기를 저장소에 묻는 쪽으로
    되돌아갈 수밖에 없고, 그 순간 첨부가 통째로 내려와 격리가 뚫린다."""
    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            bowl="attachments", path="attach_file/a.pdf", status="올림",
            summary="s", reason="r", body="b", ctx=_ctx("alice"),
        )
    assert "bytes" in str(exc.value)


def test_attachments_are_isolated_per_user(tmp_path):
    """요청마다 사용자 폴더가 다르다 — paths가 안 타면 남의 첨부 이력을 읽는다."""
    rs.namu_record(
        bowl="attachments", path="attach_file/내파일.pdf", bytes=1,
        status="올림", summary="s", reason="r", body="b", ctx=_ctx("alice"),
    )
    assert rs.namu_search(bowl="attachments", ctx=_ctx("bob"))["count"] == 0
    assert rs.namu_search(bowl="attachments", ctx=_ctx("alice"))["count"] == 1


def test_bowl_is_mandatory(tmp_path):
    """그릇을 안 적으면 거절한다 — 옛 코어는 조용히 교훈으로 보냈고, 잘못 담겨도
    아무도 모르는 그 경로가 이번에 없애려는 결함이다."""
    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            summary="s", reason="r", body="b", topic="t", ctx=_ctx("alice"),
        )
    assert "bowl" in str(exc.value)
    assert not (tmp_path / "users" / "alice" / "memory").exists()


# ---------------------------------------------------------------------------
# 작업일지(tasks) — 회원 폴더의 tasks/<프로젝트>/<작업>/
#
# namu-68은 이 그릇을 거절했었다. 근거로 적혀 있던 "코어가 컨테이너 홈에 쓰므로
# 갈아끼울 자리가 없다"는 코어의 한 함수(tasks_root_for)에만 해당하는 사실이었고,
# 이 서버는 그 함수를 부르지 않는다. 아래 시험들은 그 사실을 **파일 위치로** 고정한다
# — 기록이 회원 폴더 안에 생기고, 컨테이너 홈(.namu)에는 아무것도 안 생긴다.
# ---------------------------------------------------------------------------
@pytest.fixture
def _fake_home(monkeypatch, tmp_path):
    """컨테이너 홈을 빈 임시 폴더로 바꿔 둔다 — 홈 오염을 실제로 검사하기 위한
    관문이다(개인용 코어 함수가 실수로 끼어들면 여기에 `.namu`가 생긴다)."""
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# 웹에서 만든 작업이 모이는 기본 방(project_policy.WEB_PROJECT).
_WEB = "web-project"


def _make_task(user="alice", slug="namu-99-demo", **kw):
    """작업 하나를 만든다 — 방은 `web-project`를 골라 준 것으로 친다.

    이 주소에서 새 작업을 만들 때, 방이 안 정해졌으면 서버는 만들지 않고 방 목록을
    돌려준다(project_policy). 그 목록에서 회원이 고르는 걸음을 시험마다 흉내내지
    않도록, 방이 관심사가 아닌 시험은 여기서 기본 자리를 골라 준다.
    """
    params = dict(
        bowl="tasks", create=True, project=_WEB, topic=slug,
        summary="시험용 작업", reason="시험을 위해 만든 작업",
        body="다음에 시작할 지점", ctx=_ctx(user),
    )
    params.update(kw)
    return rs.namu_record(**params)


def _seed_task_folder(store_root: Path, user: str, project: str, slug: str) -> Path:
    """다른 프로젝트의 작업을 회원 폴더에 직접 심는다.

    회원 저장소에는 내 PC에서 만들어져 동기화돼 온 방들도 함께 있다 — 이 주소에서는
    그런 방을 새로 만들 수 없으므로 파일로 심는다.
    """
    task_dir = store_root / "users" / user / "tasks" / project / slug
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text(f"# {slug} — 심어 둔 작업\n", encoding="utf-8")
    (task_dir / "log.md").write_text(
        f"# log — {slug}\n\n[시작] 2026-08-01 09:00:00 hp · 심어 둔 작업\n",
        encoding="utf-8",
    )
    return task_dir


def test_task_create_writes_into_user_folder_not_container_home(tmp_path, _fake_home):
    result = _make_task()

    task_dir = tmp_path / "users" / "alice" / "tasks" / _WEB / "namu-99-demo"
    assert (task_dir / "task.md").exists()
    assert (task_dir / "log.md").exists()
    assert "시험을 위해 만든 작업" in (task_dir / "task.md").read_text(encoding="utf-8")
    log = (task_dir / "log.md").read_text(encoding="utf-8")
    assert "[시작]" in log and "[다음] " in log
    assert "다음에 시작할 지점" in log
    assert "namu-99-demo" in result
    # 컨테이너 홈은 손대지 않는다 — 이게 namu-68이 걱정했던 바로 그 지점이다.
    assert not (_fake_home / ".namu").exists()


def test_task_log_line_is_appended(tmp_path, _fake_home):
    _make_task()
    written = rs.namu_record(
        bowl="tasks", project=_WEB, topic="namu-99-demo",
        summary="1단계를 끝냈다", status="단계", reason="시험이 통과했다",
        body="생략", ctx=_ctx("alice"),
    )
    log = (
        tmp_path / "users" / "alice" / "tasks" / _WEB / "namu-99-demo" / "log.md"
    ).read_text(encoding="utf-8")
    assert "[단계]" in log and "1단계를 끝냈다" in log
    assert "    왜: 시험이 통과했다" in log, "3층(왜) 줄이 들여쓰기로 붙지 않았다"
    assert "상세: 생략" not in log, "'생략'은 줄을 만들지 않아야 한다"
    assert "1단계를 끝냈다" in written


def test_recall_returns_open_tasks(_fake_home):
    _make_task()
    result = rs.namu_recall(ctx=_ctx("alice"))
    slugs = [t["slug"] for t in result["tasks"]]
    assert slugs == ["namu-99-demo"]
    assert result["tasks"][0]["next"] == "다음에 시작할 지점"
    assert result["tasks"][0]["project"] == _WEB


def test_search_tasks_bowl_finds_log_line(_fake_home):
    _make_task()
    rs.namu_record(
        bowl="tasks", project=_WEB, topic="namu-99-demo",
        summary="검색으로 찾을 줄", status="기록", reason="생략", body="생략",
        ctx=_ctx("alice"),
    )
    result = rs.namu_search(query="검색으로", bowl="tasks", ctx=_ctx("alice"))
    assert result["bowl"] == "tasks"
    assert result["count"] == 1
    assert result["results"][0]["task_slug"] == "namu-99-demo"


def test_search_tasks_bowl_uses_the_core_word_rule(_fake_home):
    """작업일지 검색도 개인용과 같은 낱말 규칙을 쓴다 — 낱말별 AND, 순서 무관.

    이 시험이 있는 이유: 이 분기는 회원별 폴더를 훑느라 코어의 search_bowl을 못
    부르고 함수를 복사해 왔는데, 그때 **거르는 규칙까지 함께 복사돼** 코어가 낱말
    AND로 개선됐을 때(fts5-memo-tasks-index) 다섯 그릇 중 작업일지 하나만 옛
    방식(검색어를 통째로 부분일치)으로 남았다 — 나머지 넷은 코어를 부르므로
    자동으로 따라갔다. "셀프호스팅에서 되는 것이 여기서 안 되면 안 된다"가 이
    서버의 규약이고, 그 규약이 실제로 깨졌던 자리다.
    """
    _make_task()
    rs.namu_record(
        bowl="tasks", project=_WEB, topic="namu-99-demo",
        summary="검색 인덱스 설계 문서", status="기록", reason="생략", body="생략",
        ctx=_ctx("alice"),
    )
    # 낱말 순서가 달라도 걸린다(옛 방식이면 아래 둘째가 0건이 된다).
    assert rs.namu_search(query="설계 문서", bowl="tasks", ctx=_ctx("alice"))["count"] == 1
    assert rs.namu_search(query="문서 설계", bowl="tasks", ctx=_ctx("alice"))["count"] == 1
    # 한 낱말이라도 없으면 안 걸린다(AND이지 OR이 아니다).
    assert rs.namu_search(query="설계 첨부", bowl="tasks", ctx=_ctx("alice"))["count"] == 0


def test_search_tasks_bowl_finds_the_task_doc(tmp_path, _fake_home):
    """작업 설명서(task.md)도 같은 그릇에서 찾힌다 — 개인용과 같은 몫(2026-08-08).

    전에는 검색이 log.md만 봐서 "그 작업이 뭐였지"(제목·목적·완료조건)가 낱말로
    안 찾혔다. 이 분기는 회원별 폴더를 훑느라 코어 함수를 통째로는 못 부르지만,
    **설명서를 해석하는 규칙은 코어의 `parse_task_doc`을 부른다** — 바로 위 시험이
    적어 둔 사고(규칙을 베껴 와 두 서버가 갈라진 일)를 되풀이하지 않기 위해서다.
    """
    _make_task()
    task_md = (
        tmp_path / "users" / "alice" / "tasks" / _WEB / "namu-99-demo" / "task.md"
    )
    task_md.write_text(
        task_md.read_text(encoding="utf-8") + "- [ ] 설명서에만있는조건\n",
        encoding="utf-8",
    )

    got = rs.namu_search(query="설명서에만있는조건", bowl="tasks", ctx=_ctx("alice"))
    assert got["count"] == 1
    assert got["results"][0]["tag"] == task_resolve.TASK_DOC_TAG
    assert got["results"][0]["task_slug"] == "namu-99-demo"


def test_closed_task_drops_out_of_open_list(_fake_home):
    _make_task()
    rs.namu_record(
        bowl="tasks", project=_WEB, topic="namu-99-demo",
        summary="다 끝냈다", status="완료", reason="생략", body="생략",
        ctx=_ctx("alice"),
    )
    assert rs.namu_recall(ctx=_ctx("alice"))["tasks"] == []


def test_tasks_are_isolated_between_users(tmp_path, _fake_home):
    _make_task(user="alice")
    assert rs.namu_recall(ctx=_ctx("bob"))["tasks"] == []
    # 조회(검색)로도 남의 기록이 새지 않는다 — 브리핑만 막고 검색이 열려 있으면
    # 격리가 아니다.
    assert rs.namu_search(bowl="tasks", ctx=_ctx("bob"))["count"] == 0
    assert rs.namu_search(bowl="tasks", ctx=_ctx("alice"))["count"] >= 1
    assert not (tmp_path / "users" / "bob" / "tasks").exists()


def test_search_tasks_project_filter(tmp_path, _fake_home):
    """조회는 방을 가려 볼 수 있다 — 회원 저장소에는 내 PC에서 동기화돼 온 방들이
    web-project와 나란히 있기 때문이다(만들기만 web-project로 모인다)."""
    _make_task(slug="alpha-one")
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "beta-one")

    merged = rs.namu_search(bowl="tasks", ctx=_ctx("alice"))
    only = rs.namu_search(bowl="tasks", project="onnamu-project", ctx=_ctx("alice"))
    assert {r["project"] for r in merged["results"]} == {_WEB, "onnamu-project"}
    assert {r["project"] for r in only["results"]} == {"onnamu-project"}


def test_task_record_requires_project(_fake_home):
    """웹에는 '지금 이 폴더'가 없다 — 프로젝트를 안 주면 어디에 쓸지 정할 수 없다."""
    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            bowl="tasks", topic="namu-99-demo", summary="한 줄",
            reason="생략", body="생략", ctx=_ctx("alice"),
        )
    assert "project" in str(exc.value)


def test_project_name_cannot_escape_user_folder(tmp_path, _fake_home):
    """덧붙이기는 여전히 보내온 이름이 곧 폴더 이름이 된다 — 경로 이탈 차단.

    만들기 쪽은 이름을 아예 안 쓰므로(언제나 web-project) 이 길이 남은 유일한
    입구다.
    """
    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            bowl="tasks", project="../../bob", topic="namu-99-demo",
            summary="한 줄", status="기록", reason="생략", body="생략",
            ctx=_ctx("alice"),
        )
    assert "project" in str(exc.value)
    assert not (tmp_path / "users" / "bob").exists()
    assert not (tmp_path / "bob").exists()


def test_closing_synonym_is_rejected(_fake_home):
    """'종료'는 닫는 말이 아니다 — 저장은 되는데 목록에는 계속 열려 있게 된다."""
    _make_task()
    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            bowl="tasks", project=_WEB, topic="namu-99-demo",
            summary="끝냈다", status="종료", reason="생략", body="생략",
            ctx=_ctx("alice"),
        )
    assert "완료" in str(exc.value) and "중단" in str(exc.value)


def test_closing_with_unmet_done_when_warns(_fake_home):
    _make_task(done_when=["실측 한 바퀴", "배포"])
    written = rs.namu_record(
        bowl="tasks", project=_WEB, topic="namu-99-demo",
        summary="여기서 접는다", status="중단", reason="생략", body="생략",
        ctx=_ctx("alice"),
    )
    assert "안 채운 완료조건 2개" in written
    assert "실측 한 바퀴" in written


def test_task_name_prefix_match(_fake_home):
    """작업 이름 앞부분만 줘도 찾는다(개인용과 같은 규칙)."""
    _make_task()
    written = rs.namu_record(
        bowl="tasks", project=_WEB, topic="namu-99",
        summary="앞부분만 줬다", status="기록", reason="생략", body="생략",
        ctx=_ctx("alice"),
    )
    assert "앞부분만 줬다" in written


def test_unknown_task_is_not_created_silently(tmp_path, _fake_home):
    """없는 이름으로 부르면 폴더를 새로 만들지 않는다 — 목적 없는 유령 작업 방지."""
    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            bowl="tasks", project=_WEB, topic="없는작업",
            summary="한 줄", status="기록", reason="생략", body="생략",
            ctx=_ctx("alice"),
        )
    assert "찾을 수 없습니다" in str(exc.value)
    assert not (tmp_path / "users" / "alice" / "tasks" / _WEB / "없는작업").exists()


def test_task_create_refuses_to_overwrite(_fake_home):
    _make_task()
    with pytest.raises(ValueError) as exc:
        _make_task()
    assert "이미 있습니다" in str(exc.value)


# ---------------------------------------------------------------------------
# 새 작업이 들어갈 자리 — 회원이 방 목록에서 고른다
#
# 이 주소는 project를 매번 자유 텍스트로 받았고(cwd가 없어서다), 그 글자를 AI가
# 회원에게 묻지 않고 지어낼 수 있었다 — 실사고: 회원이 아이디어를 기록해 달라고만
# 했는데 'blog-summary-bot'이라는 프로젝트가 확인 없이 생겼다. 그 값을 검사하는
# 게이트(확인 칸·질문 문안·15초 문턱)를 두 판 만들었고 두 판 다 뚫렸다. 지금은
# 검사하지 않고, 그 글자가 **새 폴더를 만들 수 없게** 했다(코어 project_policy) —
# 방이 안 정해졌으면 목록을 돌려주고, 목록에는 이미 있는 방과 web-project뿐이다.
# ---------------------------------------------------------------------------


def test_task_create_can_pick_an_existing_room(tmp_path, _fake_home):
    """이미 있는 방 이름을 주면 그 방에 만든다 — 새 폴더가 생기지 않으므로 그 이름으로
    할 수 있는 일은 방을 고르는 것뿐이고, 일지를 덧붙이는 일과 같은 무게다."""
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "beta-one")

    result = rs.namu_record(
        bowl="tasks", create=True, project="onnamu-project", topic="idea-0",
        summary="시험용", reason="이미 있는 방 고르기", body="생략", ctx=_ctx("alice"),
    )
    assert (
        tmp_path / "users" / "alice" / "tasks" / "onnamu-project" / "idea-0" / "task.md"
    ).exists()
    assert not (tmp_path / "users" / "alice" / "tasks" / _WEB).exists()
    assert "idea-0" in result


def test_task_create_unknown_name_asks_with_a_room_list(tmp_path, _fake_home):
    """처음 보는 이름을 넘기면 아무것도 만들지 않고 방 목록을 돌려준다 — 실사고
    'blog-summary-bot'이 정확히 이 경로였다."""
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "beta-one")

    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            bowl="tasks", create=True, project="blog-summary-bot", topic="idea-1",
            summary="시험용", reason="목록 확인", body="생략", ctx=_ctx("alice"),
        )
    msg = str(exc.value)
    # 번호 매긴 목록 = 이 회원의 방들 + web-project. 고르기만 하면 된다.
    assert "1. onnamu-project" in msg
    assert f"2. {_WEB}" in msg
    assert "회원" in msg
    # 목록에 '새 프로젝트로 만들기'가 있으면 붙은 AI가 그것을 골라 사고가 재현된다
    # (2차 게이트가 뚫린 자리가 정확히 거기였다).
    assert "새 프로젝트로 만들기" not in msg
    assert not (tmp_path / "users" / "alice" / "tasks" / "blog-summary-bot").exists()
    assert not (tmp_path / "users" / "alice" / "tasks" / _WEB).exists()


def test_task_create_without_project_asks_too(tmp_path, _fake_home):
    """이름을 아예 안 줘도 마찬가지로 목록을 돌려준다 — 자리를 정하는 것은 회원이다.

    예전에는 여기서 "project를 명시하라"고만 거절했고, 그 빈자리를 AI가 지어낸
    이름으로 채운 것이 사고였다. 지금은 채울 이름 대신 고를 목록을 준다.
    """
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "beta-one")

    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            bowl="tasks", create=True, topic="idea-2",
            summary="시험용", reason="목록 확인", body="생략", ctx=_ctx("alice"),
        )
    msg = str(exc.value)
    assert "onnamu-project" in msg and _WEB in msg
    assert not (tmp_path / "users" / "alice" / "tasks" / _WEB).exists()


def test_task_create_after_choosing_web_project(tmp_path, _fake_home):
    """목록에서 web-project를 골라 다시 부르면 그 방에 만들어진다 — 그 폴더가 아직
    없어도 된다(웹에서 만든 작업이 모이는 기본 자리라 첫 작업도 여기로 온다)."""
    result = rs.namu_record(
        bowl="tasks", create=True, project=_WEB, topic="idea-3",
        summary="시험용", reason="골라서 만들기", body="생략", ctx=_ctx("alice"),
    )
    assert (tmp_path / "users" / "alice" / "tasks" / _WEB / "idea-3" / "task.md").exists()
    assert "idea-3" in result


def test_web_project_is_a_room_per_member(tmp_path, _fake_home):
    """web-project는 회원 폴더 **안**의 이름이라 회원마다 제 방이다 — 이름이 같아도
    남의 기록이 섞이면 격리가 깨진다."""
    _make_task(user="alice", slug="alice-one")
    _make_task(user="bob", slug="bob-one")

    assert (tmp_path / "users" / "alice" / "tasks" / _WEB / "alice-one").exists()
    assert (tmp_path / "users" / "bob" / "tasks" / _WEB / "bob-one").exists()
    assert not (tmp_path / "users" / "alice" / "tasks" / _WEB / "bob-one").exists()
    assert [t["slug"] for t in rs.namu_recall(ctx=_ctx("bob"))["tasks"]] == ["bob-one"]


def test_existing_room_names_do_not_leak_between_members(tmp_path, _fake_home):
    """'이미 있는 방'은 그 회원의 방만 센다 — 남의 방 이름을 대면 만들어지지 않고
    제 방 목록이 돌아온다(방 목록이 새면 격리가 깨진다)."""
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "alpha-one")

    with pytest.raises(ValueError) as exc:
        rs.namu_record(
            bowl="tasks", create=True, project="onnamu-project", topic="idea-9",
            summary="시험용", reason="남의 방 이름", body="생략", ctx=_ctx("bob"),
        )
    # bob이 고를 수 있는 방은 제 web-project 하나뿐이다 — alice의 방이 목록에 끼면
    # 이름만으로도 남의 저장소 구성이 새어 나간다. (문두에 되울리는 것은 bob이 방금
    # 준 글자라 유출이 아니다 — 그래서 목록 줄로 확인한다.)
    msg = str(exc.value)
    assert f"1. {_WEB}" in msg
    assert "2. " not in msg
    assert not (tmp_path / "users" / "bob" / "tasks" / "onnamu-project").exists()
    assert not (
        tmp_path / "users" / "alice" / "tasks" / "onnamu-project" / "idea-9"
    ).exists()


def test_task_record_pushes_to_user_repo(monkeypatch, _fake_home):
    """작업일지도 다른 그릇과 똑같이 기록 직후 회원 저장소로 올라가야 한다."""
    calls = []
    monkeypatch.setattr(
        ur, "push", lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: calls.append(key)
    )
    _make_task()
    assert calls == ["alice"]


# ---------------------------------------------------------------------------
# 작업 옮기기 (namu_task_move) — new-project-rule 설계서 4단계의 클라우드 판.
#
# 판정 로직(어느 방으로 옮길 수 있는지, 폴더를 실제로 옮기는 절차)은 코어
# `task_move` 모듈을 그대로 부른다(routing_server.py 도구 정의 위 주석 참고) —
# 여기서는 그 판정을 다시 구현하지 않고, **클라우드에서만 뜻이 있는 것**(회원
# 격리·project 필수·push 배선)만 겨눈다.
# ---------------------------------------------------------------------------
def test_task_move_unknown_room_rejected(tmp_path, _fake_home):
    """없는 방으로 옮기려 하면 거절되고, 그 회원의 방 목록이 그대로 돌아온다."""
    _make_task(user="alice", slug="alice-one")

    with pytest.raises(ValueError) as exc:
        rs.namu_task_move(task="alice-one", to="ghost-room", project=_WEB, ctx=_ctx("alice"))
    msg = str(exc.value)
    assert f"1. {_WEB}" in msg
    assert "2. " not in msg

    # 아무것도 옮기지 않았다.
    assert (tmp_path / "users" / "alice" / "tasks" / _WEB / "alice-one").exists()


def test_task_move_destination_isolated_between_members(tmp_path, _fake_home):
    """다른 회원의 방 이름을 목적지로 주면 거절되고, 그 이름이 목록에도 안 나온다
    (격리 시험 — 이 도구의 급소). `bob-room`은 실제로 존재하는 방이지만 bob의
    것이라, alice에게는 없는 이름과 똑같이 취급돼야 한다."""
    _make_task(user="alice", slug="alice-one")
    _seed_task_folder(tmp_path, "alice", "alice-second", "alice-two")
    _seed_task_folder(tmp_path, "bob", "bob-room", "bob-one")

    with pytest.raises(ValueError) as exc:
        rs.namu_task_move(task="alice-one", to="bob-room", project=_WEB, ctx=_ctx("alice"))
    msg = str(exc.value)
    # 문두에 되울리는 "방 'bob-room'는 없습니다"는 alice가 방금 준 글자라 유출이
    # 아니다(test_existing_room_names_do_not_leak_between_members와 같은 이유) —
    # 그래서 목록 줄(번호가 붙은 줄)만으로 확인한다. alice의 방 둘만 번호로 뜨고
    # bob-room은 목록 줄로는 전혀 안 나와야 한다.
    listed = [line.strip() for line in msg.splitlines() if line.strip()[:2].rstrip(".").isdigit()]
    # known은 sorted()로 만들어지므로 순서도 고정이다: 'alice-second' < 'web-project'.
    assert listed == ["1. alice-second", f"2. {_WEB}"]
    assert not any("bob-room" in line for line in listed)

    # alice 쪽에는 bob의 방이 생기지 않았고, alice의 작업도 그대로다.
    assert not (tmp_path / "users" / "alice" / "tasks" / "bob-room").exists()
    assert (tmp_path / "users" / "alice" / "tasks" / _WEB / "alice-one").exists()
    # bob의 방은 손대지 않았다.
    assert (tmp_path / "users" / "bob" / "tasks" / "bob-room" / "bob-one").exists()


def test_task_move_normal(tmp_path, _fake_home):
    """정상 이동: 그 회원의 A방 작업이 B방으로 옮겨진다."""
    _make_task(user="alice", slug="alice-one")
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "placeholder")

    result = rs.namu_task_move(
        task="alice-one", to="onnamu-project", project=_WEB, ctx=_ctx("alice")
    )

    assert not (tmp_path / "users" / "alice" / "tasks" / _WEB / "alice-one").exists()
    dest_dir = tmp_path / "users" / "alice" / "tasks" / "onnamu-project" / "alice-one"
    assert dest_dir.exists()
    assert "alice-one" in str(result)
    log = (dest_dir / "log.md").read_text(encoding="utf-8")
    assert "[기록]" in log
    assert _WEB in log and "onnamu-project" in log


def test_task_move_without_source_project_rejected(_fake_home):
    """원본 방(project)을 안 주면 거절된다 — 웹에는 '지금 이 폴더'가 없다."""
    with pytest.raises(ValueError) as exc:
        rs.namu_task_move(task="whatever", to=_WEB, ctx=_ctx("alice"))
    assert "project" in str(exc.value)


def test_task_move_destination_slug_conflict_rejected(tmp_path, _fake_home):
    """목적지에 같은 이름 작업이 이미 있으면 거절되고 원본이 그대로 남는다."""
    _make_task(user="alice", slug="alice-one")
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "alice-one")

    with pytest.raises(ValueError) as exc:
        rs.namu_task_move(
            task="alice-one", to="onnamu-project", project=_WEB, ctx=_ctx("alice")
        )
    assert "이미" in str(exc.value)

    # 원본은 그대로, 목적지에 원래 있던 것도 손대지 않았다(합치지 않는다).
    assert (tmp_path / "users" / "alice" / "tasks" / _WEB / "alice-one").exists()
    assert (
        tmp_path / "users" / "alice" / "tasks" / "onnamu-project" / "alice-one"
    ).exists()


def test_task_move_pushes_to_user_repo(monkeypatch, tmp_path, _fake_home):
    """옮긴 뒤에도 다른 쓰기 도구와 같이 곧바로 회원 저장소로 push를 시도한다."""
    _make_task(user="alice", slug="alice-one")
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "placeholder")

    calls = []
    monkeypatch.setattr(
        ur, "push", lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: calls.append(key)
    )
    rs.namu_task_move(task="alice-one", to="onnamu-project", project=_WEB, ctx=_ctx("alice"))
    assert calls == ["alice"]


def test_task_move_push_failure_still_succeeds_with_warning(monkeypatch, tmp_path, _fake_home):
    """push가 실패해도 이동 자체는 이미 끝났으니 raise하지 않고 경고로 담는다
    (다른 쓰기 도구들과 같은 배선, `_push_and_collect_warning`)."""
    _make_task(user="alice", slug="alice-one")
    _seed_task_folder(tmp_path, "alice", "onnamu-project", "placeholder")

    def _boom(conn, key, message=ur.DEFAULT_COMMIT_MESSAGE):
        raise ur.PushRejected("dummy push rejected for test")

    monkeypatch.setattr(ur, "push", _boom)
    result = rs.namu_task_move(
        task="alice-one", to="onnamu-project", project=_WEB, ctx=_ctx("alice")
    )

    assert isinstance(result, dict)
    assert "dummy push rejected for test" in result.get("warning", "")
    # 이동 자체는 성공했다 — push 실패가 되돌리지 않는다.
    assert (
        tmp_path / "users" / "alice" / "tasks" / "onnamu-project" / "alice-one"
    ).exists()


def test_search_rejects_project_on_learnings_bowl(_fake_home):
    """조용히 무시하면 부른 쪽이 걸러진 줄 알고 잘못된 결론을 낸다."""
    with pytest.raises(ValueError) as exc:
        rs.namu_search(query="아무거나", project="namu-agent", ctx=_ctx("alice"))
    assert "전용 축" in str(exc.value)


def test_old_field_names_still_work_and_say_where_they_went(tmp_path):
    """옛 이름(kind/subject/statement/source)으로 부르는 호출자가 이미 돌고 있다 —
    거절하지 않고 새 칸으로 옮겨 저장하되, 어디로 옮겼는지 반드시 알린다(옮겨놓고
    알리지 않으면 그것도 조용한 유실이다).

    3층이 다 필요하다는 규칙 자체는 옛 이름으로 불러도 그대로 적용된다(원문 칸이
    비면 '생략' 한 단어를 넣어야 한다) — 개인용 서버와 같은 판정이다."""
    result = rs.namu_record(
        kind="fact", subject="alice", statement="한국어 선호", source="본인 발화",
        body="생략", ctx=_ctx("alice"),
    )
    assert isinstance(result, dict), "옛 이름 호출에는 옮긴 내역이 함께 와야 한다"
    assert isinstance(result["id"], str) and result["id"]
    assert result["notices"], "어디로 옮겼는지 알리지 않았다"

    text = _yaml_text(tmp_path, "alice", "profile.yaml")
    assert "한국어 선호" in text  # statement → summary
    assert "본인 발화" in text     # source → reason


def test_stale_cache_from_old_core_is_rebuilt_not_crashed(tmp_path):
    """옛 코어(v0.1.29)가 만들어 둔 캐시에는 summary/body 컬럼이 없다. 새 코어가
    그 파일을 그대로 쓰면 `no such column`으로 깨지므로, 스키마가 낡으면 yaml에서
    자동 재생성돼야 한다 — 이미 저장돼 있던 옛 항목도 그대로 살아 있어야 한다."""
    user_dir = tmp_path / "users" / "alice"
    (user_dir / "memory").mkdir(parents=True)
    (user_dir / "db").mkdir(parents=True)
    (user_dir / "memory" / "learnings.yaml").write_text(
        "id: OLDENTRY0000000000000000\n"
        "timestamp: '2026-07-18T00:00:00+00:00'\n"
        "task: 옛 코어가 남긴 항목\n"
        "task_type: other\n"
        "outcome: success\n"
        "reason: 옛 이유\n"
        "machine: hp\n"
        "verified_by: ai\n"
        "tags: []\n"
        "kind: lesson\n"
        "via: claude\n",
        encoding="utf-8",
    )
    old_db = user_dir / "db" / "namu.db"
    with closing(sqlite3.connect(old_db)) as conn:
        with conn:
            conn.execute(
                "CREATE TABLE learnings (id TEXT PRIMARY KEY, timestamp TEXT, "
                "task TEXT, task_type TEXT, outcome TEXT, reason TEXT, "
                "machine TEXT, verified_by TEXT, tags TEXT, kind TEXT, via TEXT)"
            )
            conn.execute(
                "INSERT INTO learnings VALUES "
                "('OLDENTRY0000000000000000','2026-07-18T00:00:00+00:00',"
                "'옛 코어가 남긴 항목','other','success','옛 이유','hp','ai','[]',"
                "'lesson','claude')"
            )

    result = rs.namu_recall(ctx=_ctx("alice"))

    tasks = [d["task"] for d in result["learnings"]]
    assert "옛 코어가 남긴 항목" in tasks, "옛 항목이 재생성 과정에서 사라졌다"
    with closing(sqlite3.connect(old_db)) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(learnings)")}
    assert {"summary", "body"} <= cols, "낡은 스키마가 그대로 남았다"


def test_vendored_core_supports_three_layer_storage():
    """코어 핀이 3층 이전(v0.1.41 미만)으로 되돌아가면 여기서 잡는다.

    이 가드가 필요한 이유: 겉 버전만 올리고 코어 핀을 확인하지 않아 2주 지난
    코어가 배포됐고, 그 사실을 사용자가 실제로 기억을 저장해 본 뒤에야 알았다.
    """
    import db as core_db
    import record_input as core_record_input

    assert {"summary", "body"} <= set(core_db._COLS)
    assert hasattr(core_record_input, "normalize")


# ---------------------------------------------------------------------------
# validate_settings / AuthMiddleware / _build_transport_security — 순수 로직만
# 테스트(mcp_server import 없이, vendor/namu-agent/namu-plugin/test_http_server.py
# 방식 참고). mcp_server가 아닌 routing_server 모듈 자체를 대상으로 한다.
# ---------------------------------------------------------------------------
async def _dummy_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _settings(**overrides) -> dict:
    base = {
        "token": "",
        "path_secret": "",
        "host": "127.0.0.1",
        "port": 8770,
        "pull_interval": 60.0,
        "allow_noauth": False,
        "allowed_hosts": [],
    }
    base.update(overrides)
    return base


def test_validate_settings_never_blocks_startup():
    """namu-59: 환경변수 인증 설정이 하나도 없어도 기동을 막지 않는다.

    예전에는 "token도 path_secret도 없으면 무인증 노출"이라 기동을 거부했다.
    지금은 모든 MCP 요청이 사용자별 열쇠 검사를 반드시 통과해야 하므로
    (열쇠 없으면 404) 무인증 노출 자체가 성립하지 않는다. 그 보장은 이 함수가
    아니라 아래 _PerUserSecretDispatcher 테스트들이 지킨다.
    """
    rs.validate_settings(_settings())  # SystemExit 없이 통과
    rs.validate_settings(_settings(allow_noauth=True))
    rs.validate_settings(_settings(token="t"))


def test_auth_middleware_x_api_key_match():
    app = rs.AuthMiddleware(_dummy_app, token="tok123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"x-api-key": "tok123"})
    assert r.status_code == 200


def test_auth_middleware_x_api_key_mismatch():
    app = rs.AuthMiddleware(_dummy_app, token="tok123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"x-api-key": "wrong"})
    assert r.status_code == 401


def test_auth_middleware_bearer_match():
    app = rs.AuthMiddleware(_dummy_app, token="tok123")
    client = TestClient(app)
    r = client.get("/mcp", headers={"Authorization": "Bearer tok123"})
    assert r.status_code == 200


def test_auth_middleware_no_header_rejected():
    app = rs.AuthMiddleware(_dummy_app, token="tok123")
    client = TestClient(app)
    r = client.get("/mcp")
    assert r.status_code == 401


def test_auth_middleware_no_token_configured_passes_through():
    app = rs.AuthMiddleware(_dummy_app, token="")
    client = TestClient(app)
    r = client.get("/mcp")
    assert r.status_code == 200


def test_build_transport_security_empty_returns_none():
    assert rs._build_transport_security([]) is None


def test_build_transport_security_star_disables_protection():
    settings = rs._build_transport_security(["*"])
    assert settings.enable_dns_rebinding_protection is False


def test_build_transport_security_adds_to_localhost_defaults_not_replaces():
    settings = rs._build_transport_security(["namu-cloud.onnamu.kr"])
    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert "localhost:*" in settings.allowed_hosts
    assert "[::1]:*" in settings.allowed_hosts
    assert "namu-cloud.onnamu.kr" in settings.allowed_hosts


# ---------------------------------------------------------------------------
# resolve_streamable_path — path_secret(URL 경로 시크릿) 기반 헤더 없는 인증.
# claude.ai 웹 커스텀 커넥터는 임의 헤더(x-api-key)를 못 넣고 URL만 받으므로,
# 개인용 NAMU처럼 시크릿을 URL 경로(/mcp/<secret>)에 실어 인증한다.
# ---------------------------------------------------------------------------
def test_resolve_streamable_path_without_secret_stays_mcp():
    assert rs.resolve_streamable_path(_settings()) == "/mcp"


def test_resolve_streamable_path_ignores_shared_path_secret():
    """namu-59: 전원 공용 열쇠(NAMU_HTTP_PATH_SECRET)는 더 이상 경로에 실리지
    않는다. 설정돼 있어도 마운트 경로는 고정이다 — 이 값을 되살려 경로에 넣으면
    "열쇠 하나를 전원이 돌려쓰는" 예전 구멍이 그대로 돌아온다."""
    assert rs.resolve_streamable_path(_settings(path_secret="s3cr3t")) == "/mcp"


def test_build_app_ignores_shared_path_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    monkeypatch.setenv("NAMU_HTTP_PATH_SECRET", "s3cr3t")
    rs.build_app()
    assert rs.mcp.settings.streamable_http_path == "/mcp"


def test_build_app_without_path_secret_keeps_mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    monkeypatch.delenv("NAMU_HTTP_PATH_SECRET", raising=False)
    rs.build_app()
    assert rs.mcp.settings.streamable_http_path == "/mcp"


def test_build_app_actually_wires_the_ticket_addresses(monkeypatch, tmp_path):
    """대역 앱이 아니라 **진짜로 조립된 앱**에서 티켓 주소가 열리는지 본다.

    갈림길 단위 시험은 대역 세 개로 분기만 보므로, build_app이 티켓 앱을 실제로
    끼워 넣는 것을 잊어도 통과한다 — 그러면 회원이 링크를 눌렀을 때 인증에 막혀
    404가 나고, 그 사실은 배포한 뒤에야 드러난다.
    """
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")

    client = TestClient(rs.build_app())
    r = client.get("/u/그런번호없음", headers={"accept": "text/html"})

    # 인증에 걸렸다면 401이었을 것이다. 404는 티켓 앱이 답했다는 뜻이다.
    assert r.status_code == 404
    assert "링크" in r.text


# ---------------------------------------------------------------------------
# _AuthOrMcpDispatcher — namu-59 2차: '/auth/'는 web_auth 앱, 그 외 전부는
# MCP+Auth 앱. 여기서는 rs.build_app()이 조립하는 실제 web_auth/FastMCP 앱이
# 아니라 대역(dummy) ASGI 콜러블 두 개로 디스패치 로직 자체만 단위 검증한다
# (web_auth 라우트 자체의 동작·보안은 tests/test_web_auth.py 소관).
# ---------------------------------------------------------------------------
def _make_labelled_app(label: str):
    async def _app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": label.encode("utf-8")})

    return _app


def test_dispatcher_routes_auth_prefix_to_auth_app():
    auth_app = _make_labelled_app("auth")
    mcp_app = _make_labelled_app("mcp")
    dispatcher = rs._AuthOrMcpDispatcher(auth_app, mcp_app, _make_labelled_app("ticket"))
    client = TestClient(dispatcher)
    r = client.get("/auth/github/login")
    assert r.text == "auth"


def test_dispatcher_sends_the_ai_guide_to_the_web_app():
    """AI 안내원(namu-ai-guide 5단계)의 주소가 MCP 쪽으로 새지 않는가.

    `/auth/ask`라는 이름을 고른 이유가 바로 이 검사에 있다 — 여기서 `/auth/`는
    *로그인이 필요하다*가 아니라 *웹 화면 쪽으로 보낸다*는 뜻이라, 이 이름을
    쓰면 디스패처와 공개 경로 목록을 한 글자도 안 고친다(설계서 3절). 그 전제가
    깨지면 말풍선이 인증을 요구하는 MCP 쪽으로 넘어가 조용히 죽는다.
    """
    dispatcher = rs._AuthOrMcpDispatcher(
        _make_labelled_app("auth"), _make_labelled_app("mcp"), _make_labelled_app("ticket")
    )

    assert TestClient(dispatcher).post("/auth/ask").text == "auth"


def test_dispatcher_routes_everything_else_to_mcp_app():
    auth_app = _make_labelled_app("auth")
    mcp_app = _make_labelled_app("mcp")
    dispatcher = rs._AuthOrMcpDispatcher(auth_app, mcp_app, _make_labelled_app("ticket"))
    client = TestClient(dispatcher)
    for path in ["/mcp", "/mcp/some-secret", "/authx/notreallyauth", "/auth"]:
        r = client.get(path)
        assert r.text == "mcp", f"path={path!r}가 auth_app으로 잘못 라우팅됐다"


# ---------------------------------------------------------------------------
# 공개 페이지(namu-70) — 홈·시작하기 등은 로그인 없이 열린다. 여는 방식이
# 잘못되면 인증 없이 MCP에 닿는 구멍이 되므로, 여는 범위를 여기서 못박는다.
# ---------------------------------------------------------------------------
def test_dispatcher_opens_the_public_pages_to_the_web_app():
    auth_app = _make_labelled_app("auth")
    mcp_app = _make_labelled_app("mcp")
    client = TestClient(rs._AuthOrMcpDispatcher(auth_app, mcp_app, _make_labelled_app("ticket")))

    for path in ["/", "/start", "/memory", "/safety", "/faq"]:
        assert client.get(path).text == "auth", f"공개 경로 {path!r}가 안 열렸다"


def test_public_paths_are_matched_exactly_not_by_prefix():
    """접두어로 열면 `/faq/../mcp` 같은 조작에 문이 열릴 여지가 생긴다.
    목록에 적힌 글자 그대로가 아니면 전부 닫히는 쪽(MCP+인증)으로 가야 한다."""
    auth_app = _make_labelled_app("auth")
    mcp_app = _make_labelled_app("mcp")
    client = TestClient(rs._AuthOrMcpDispatcher(auth_app, mcp_app, _make_labelled_app("ticket")))

    for path in [
        "/faq/mcp",
        "/faq/../mcp",
        "/start/mcp/secret",
        "/memory/mcp",
        # `//`는 여기 넣지 않는다 — 시험용 클라이언트가 그 주소를 `/`로
        # 정규화해 버려서, 디스패처가 아니라 클라이언트를 시험하게 된다.
        "/FAQ",
        "/faq.",
    ]:
        assert client.get(path).text == "mcp", f"{path!r}가 공개로 새어 나갔다"


def test_dispatcher_sends_ticket_paths_to_the_ticket_app():
    """티켓 주소는 번호가 주소 안에 있어 목록으로 열 수 없다 — 접두어로 가른다."""
    client = TestClient(rs._AuthOrMcpDispatcher(
        _make_labelled_app("auth"), _make_labelled_app("mcp"),
        _make_labelled_app("ticket"),
    ))

    for path in ["/u/abc123", "/d/abc123"]:
        assert client.get(path).text == "ticket", f"티켓 경로 {path!r}가 안 열렸다"


def test_ticket_prefix_never_leaks_into_the_mcp_side():
    """접두어로 가르는 쪽이 잘못 분류돼도 **인증이 걸린 쪽으로는 새지 않아야**
    한다 — 티켓 앱에는 MCP 라우트가 애초에 없다."""
    client = TestClient(rs._AuthOrMcpDispatcher(
        _make_labelled_app("auth"), _make_labelled_app("mcp"),
        _make_labelled_app("ticket"),
    ))

    # 앞자락이 아예 다른 것들은 종전대로 닫히는 쪽(MCP+인증)으로 간다.
    for path in ["/u", "/d", "/ux/abc", "/mcp"]:
        assert client.get(path).text == "mcp", f"{path!r}가 티켓 쪽으로 샜다"


def test_public_paths_and_menu_never_drift_apart():
    """문(디스패처)과 메뉴(화면)가 같은 목록을 봐야 한다 — 두 곳에 손으로
    적으면 메뉴에는 있는데 눌러도 404가 나는 항목이 생긴다."""
    import ui

    assert rs._PUBLIC_PATHS == frozenset(path for path, _label in ui.MENU)


def test_dispatcher_default_is_the_authenticated_side():
    """디스패처 생성 시 mcp_app 자리에 실제 AuthMiddleware를 넣으면, '/auth/'가
    아닌 모든 요청이 여전히 인증을 요구해야 한다 — 기본값이 안전한 쪽인지
    회귀 방지로 한 번 더 확인(위 테스트는 대역 앱이라 인증 자체는 검증 못 함)."""
    auth_app = _make_labelled_app("auth")
    mcp_app = rs.AuthMiddleware(_dummy_app, token="tok123")
    dispatcher = rs._AuthOrMcpDispatcher(auth_app, mcp_app, _make_labelled_app("ticket"))
    client = TestClient(dispatcher)
    r = client.get("/mcp")
    assert r.status_code == 401
    r2 = client.get("/mcp", headers={"x-api-key": "tok123"})
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# lifespan 스모크 — TestClient를 **컨텍스트 매니저로 열어 lifespan을 실제로
# 트리거**한다(2차 검수 지적 ⑤). 위의 모든 dispatcher/build_app 테스트는
# `client.get(...)`만 호출하는데, Starlette TestClient는 컨텍스트 매니저로
# 진입하지 않으면 ASGI lifespan(startup/shutdown) 이벤트 자체를 보내지 않는다
# — 그래서 `_AuthOrMcpDispatcher.__call__`의 `scope.get("path", "")`를
# `scope["path"]`로 바꿔 lifespan scope(“path” 키가 없다)에서 KeyError가 나게
# 만들어도 그 회귀를 하나도 못 잡았다(실측: 위 테스트들은 전부 여전히 통과).
#
# 이 테스트 하나만 실제로 `with TestClient(...) as client:`를 쓴다 — FastMCP의
# StreamableHTTPSessionManager는 `routing_server.mcp`에 귀속된 모듈 싱글턴이라
# 같은 프로세스에서 lifespan을 두 번 열면(session_manager.run()이 인스턴스당
# 한 번만 허용돼) "can only be called once per instance"로 실패한다
# (vendor/namu-agent/namu-plugin/test_http_server.py 주석·실측과 동일한 제약).
# 그래서 이 파일(및 이 프로젝트 tests/ 전체)에서 lifespan을 여는 곳은 여기
# 하나뿐이어야 한다.
# ---------------------------------------------------------------------------
def test_build_app_lifespan_starts_and_stops_mcp_session_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_STORE_ROOT", str(tmp_path))
    monkeypatch.setenv("NAMU_HTTP_ALLOW_NOAUTH", "1")
    app = rs.build_app()
    # `with` 진입 자체가 lifespan startup을 보낸다 — 디스패처가 lifespan scope를
    # 처리하다 예외를 내면 이 줄에서 바로 실패한다(요청을 굳이 보낼 필요도 없다).
    with TestClient(app) as client:
        r = client.get(
            "/mcp", headers={"Accept": "application/json, text/event-stream"}
        )
        # 정확한 상태코드(호스트 헤더 검증 등 부수 조건에 따라 달라질 수 있다)를
        # 못 박기보다, "lifespan 진입 후에도 서버가 죽지 않고 요청에 응답한다"만
        # 확인한다 — 이 테스트의 목적은 프로토콜 성공이 아니라 lifespan 배선이다.
        assert r.status_code != 500
    # `with` 블록을 정상적으로 빠져나왔다는 것 자체가 shutdown도 예외 없이
    # 끝났다는 증거다.


# ---------------------------------------------------------------------------
# 저장소 동기화 배선(namu-58 4차) — TTL 판정 순수 함수
# ---------------------------------------------------------------------------
def test_needs_sync_true_when_no_local_copy():
    # user_repo.user_dir("nobody")가 가리키는 폴더 자체가 아직 없다 —
    # 사용자 결정 1의 "단, 사본이 아직 없으면 TTL과 무관하게" 조항.
    assert rs._needs_sync("nobody", ttl_sec=99999) is True


def test_needs_sync_false_within_ttl_after_mark_synced():
    key = "alice"
    (ur.user_dir(key) / ".git").mkdir(parents=True)
    rs._mark_synced(key)
    assert rs._needs_sync(key, ttl_sec=60) is False


def test_needs_sync_true_after_ttl_elapses():
    key = "alice"
    (ur.user_dir(key) / ".git").mkdir(parents=True)
    rs._mark_synced(key)
    marker = rs._sync_marker_path(key)
    # 마커 시각을 TTL보다 더 과거로 돌려 "시간이 지났다"를 흉내낸다.
    old = marker.stat().st_mtime - 120
    os.utime(marker, (old, old))
    assert rs._needs_sync(key, ttl_sec=60) is True


def test_needs_sync_true_when_local_copy_exists_but_never_marked():
    # 사본(.git)은 있지만 마커 파일이 없는 상태 — 과거(이 배선이 없던 시절)에
    # 만들어진 사본, 혹은 마커 기록 자체가 실패했던 경우를 흉내낸다. "모르면
    # 넘어가기"가 아니라 "모르면 최신화"가 안전한 기본값이어야 한다.
    key = "alice"
    (ur.user_dir(key) / ".git").mkdir(parents=True)
    assert rs._needs_sync(key, ttl_sec=99999) is True


def test_repo_sync_ttl_sec_default_is_60(monkeypatch):
    monkeypatch.delenv("NAMU_REPO_SYNC_TTL_SEC", raising=False)
    assert rs._repo_sync_ttl_sec() == 60.0


def test_repo_sync_ttl_sec_reads_env_override(monkeypatch):
    monkeypatch.setenv("NAMU_REPO_SYNC_TTL_SEC", "5")
    assert rs._repo_sync_ttl_sec() == 5.0


def test_repo_sync_ttl_sec_rejects_non_numeric(monkeypatch):
    monkeypatch.setenv("NAMU_REPO_SYNC_TTL_SEC", "not-a-number")
    with pytest.raises(ValueError):
        rs._repo_sync_ttl_sec()


# ---------------------------------------------------------------------------
# 저장소 동기화 배선 — `_ensure_repo_synced` 오케스트레이션(요구사항 ⓐⓑ)
# ---------------------------------------------------------------------------
def test_ensure_repo_synced_skips_second_call_within_ttl(monkeypatch):
    """ⓐ TTL 안에서는 다시 fetch(ensure_ready)하지 않는다."""
    calls: list[str] = []

    def _spy(conn, key):
        calls.append(key)
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ur, "ensure_ready", _spy)
    rs._ensure_repo_synced(None, "alice")
    rs._ensure_repo_synced(None, "alice")  # 기본 TTL(60초) 이내 — 다시 부르면 안 된다
    assert calls == ["alice"], "TTL 안에서 ensure_ready가 다시 호출됐다"


def test_ensure_repo_synced_calls_again_after_ttl_elapses(monkeypatch):
    """ⓐ TTL이 지나면 다시 fetch(ensure_ready)한다."""
    calls: list[str] = []

    def _spy(conn, key):
        calls.append(key)
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ur, "ensure_ready", _spy)
    monkeypatch.setenv("NAMU_REPO_SYNC_TTL_SEC", "0")  # TTL 0 — 항상 만료된 것으로 취급
    rs._ensure_repo_synced(None, "alice")
    rs._ensure_repo_synced(None, "alice")
    assert calls == ["alice", "alice"], "TTL이 지났는데도 ensure_ready가 다시 호출되지 않았다"


def test_ensure_repo_synced_always_calls_when_no_local_copy(monkeypatch):
    """ⓑ 사본이 없으면 TTL과 무관하게 매번 ensure_ready를 부른다."""
    calls: list[str] = []

    def _spy_without_creating_git(conn, key):
        # 일부러 .git을 만들지 않는다 — "사본이 여전히 없다" 상태를 매번 재현한다
        # (예: clone이 반복 실패하는 상황을 흉내).
        calls.append(key)

    monkeypatch.setattr(ur, "ensure_ready", _spy_without_creating_git)
    rs._ensure_repo_synced(None, "bob")
    rs._ensure_repo_synced(None, "bob")
    assert calls == ["bob", "bob"], "사본이 없는데도 두 번째 호출에서 ensure_ready를 건너뛰었다"


# ---------------------------------------------------------------------------
# 저장소 동기화 배선 — ⓒ 미연결 사용자는 3도구 모두 거절
# ---------------------------------------------------------------------------
def test_all_three_tools_reject_unconnected_user(monkeypatch):
    """오토유즈 스텁을 되돌려 진짜 `user_repo.ensure_ready`(RepoNotConnected 판정
    포함)가 실행되게 한다 — 이 신원 장부에는 이 user_key를 등록한 적이 없으므로
    (`identity.upsert_user`를 부른 적이 없다) 세 도구 모두 거절해야 한다."""
    monkeypatch.setattr(ur, "ensure_ready", _REAL_ENSURE_READY)

    with pytest.raises(ValueError) as exc_recall:
        rs.namu_recall(ctx=_ctx("neveronboarded"))
    with pytest.raises(ValueError) as exc_search:
        rs.namu_search("q", ctx=_ctx("neveronboarded"))
    with pytest.raises(ValueError) as exc_record:
        rs.namu_record(
            bowl="learnings", topic="t", summary="s", reason="r", body="생략",
            status="success", ctx=_ctx("neveronboarded"),
        )

    # 세 예외 모두 user_repo.RepoNotConnected의 온보딩 안내 원문(한국어+영어)을
    # 그대로 담고 있어야 한다(사용자 결정 2 — 재사용).
    for exc in (exc_recall, exc_search, exc_record):
        message = str(exc.value)
        assert "저장소를 연결" in message
        assert "GitHub App" in message


# ---------------------------------------------------------------------------
# 저장소 동기화 배선 — ⓓⓔ record 후 push 호출, push 실패해도 기록은 성공
# ---------------------------------------------------------------------------
def test_namu_record_calls_push_after_local_write(monkeypatch):
    """ⓓ 로컬 기록이 끝난 뒤 반드시 push가 호출된다."""
    calls: list[str] = []

    def _spy_push(conn, key, message=ur.DEFAULT_COMMIT_MESSAGE):
        calls.append(key)
        return True

    monkeypatch.setattr(ur, "push", _spy_push)
    entry_id = rs.namu_record(
        bowl="learnings", topic="t", summary="s", reason="r", body="생략",
        status="success", ctx=_ctx("pusher"),
    )
    assert isinstance(entry_id, str) and entry_id
    assert calls == ["pusher"], "namu_record가 로컬 기록 후 push를 부르지 않았다"


def test_namu_record_push_failure_still_succeeds_with_warning(monkeypatch, caplog):
    """ⓔ push가 실패해도 도구 호출 자체는 실패시키지 않는다 — 기록은 로컬에
    안전히 남고, 반환값에 경고가 실리며, 실패는 logger로도 남는다(사용자 결정 3)."""

    def _boom(conn, key, message=ur.DEFAULT_COMMIT_MESSAGE):
        raise ur.PushRejected("dummy push rejected for test")

    monkeypatch.setattr(ur, "push", _boom)

    with caplog.at_level("WARNING", logger="namu.routing_server"):
        result = rs.namu_record(
            bowl="learnings", topic="t", summary="s", reason="r", body="생략",
            status="success", ctx=_ctx("pushfail"),
        )

    # 반환 모양(결정 3): 성공 경로의 기존 계약(ULID 문자열)을 깨지 않기 위해,
    # push가 실패했을 때만 {"id": ..., "warning": ...} 딕셔너리로 바뀐다.
    assert isinstance(result, dict), "push 실패 시 반환값이 dict로 바뀌지 않았다"
    assert isinstance(result.get("id"), str) and result["id"]
    assert "dummy push rejected for test" in result.get("warning", "")

    # 조용히 삼키지 않는다 — 운영자가 추적할 수 있도록 logger.warning으로도 남는다.
    assert any(
        "push 실패" in rec.message for rec in caplog.records
    ), "push 실패가 logger로 전혀 남지 않았다"

    # 기록 자체는 로컬에 안전하게 남아 있어야 한다(push 경고와 무관하게).
    recall_result = rs.namu_recall(ctx=_ctx("pushfail"))
    ids = [d["id"] for d in recall_result["learnings"]]
    assert result["id"] in ids, "push가 실패했다고 로컬 기록 자체가 사라지면 안 된다"


def test_namu_record_no_warning_on_success_keeps_plain_string_return(monkeypatch):
    """push가 성공(또는 변경 없음)하면 반환값은 여전히 순수 ULID 문자열이어야
    한다 — supersedes= 등에 반환값을 그대로 넘겨 쓰는 기존 호출자와의 호환성이
    이 배선의 최우선 기준이었다."""
    monkeypatch.setattr(
        ur, "push", lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: True
    )
    result = rs.namu_record(
        bowl="learnings", topic="t", summary="s", reason="r", body="생략",
        status="success", ctx=_ctx("pushok"),
    )
    assert isinstance(result, str) and result


# ---------------------------------------------------------------------------
# _PerUserSecretDispatcher — 이 서버의 인증 경계 (namu-59)
#
# namu-59 이전 구조의 결함: 경로에 실린 열쇠는 전원 공용이었고, "누구인가"는
# 요청자가 스스로 적어내는 `?user=` 이름표로 정해졌다. 열쇠를 아는 사람이면
# 남의 이름표를 적어 남의 서랍을 열 수 있었다. 아래 테스트들이 그 구멍이
# 되돌아오지 않는지를 지킨다 — 특히 `_overrides_client_supplied_user`.
# ---------------------------------------------------------------------------
def _seen_scope_app():
    """통과한 scope를 붙잡아 두는 더미 앱."""
    seen: dict = {}

    async def app(scope, receive, send):
        seen["scope"] = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return app, seen


def _dispatcher_get(app, path: str, query: bytes = b"") -> int:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "headers": [],
        "client": ("test", 1234),
        "server": ("testserver", 443),
        "scheme": "https",
    }
    status: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    asyncio.run(app(scope, receive, send))
    return status["code"]


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """실제 파일 장부 + 가입자 둘(alice/bob). 열쇠 두 개를 함께 돌려준다."""
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))
    with closing(identity.connect()) as conn:
        key_a = identity.upsert_user(conn, 111, "alice")
        key_b = identity.upsert_user(conn, 222, "bob")
        secret_a = identity.get_by_user_key(conn, key_a)["mcp_secret"]
        secret_b = identity.get_by_user_key(conn, key_b)["mcp_secret"]
    return {"key_a": key_a, "key_b": key_b, "secret_a": secret_a, "secret_b": secret_b}


def test_dispatcher_valid_secret_passes_and_injects_owner(ledger):
    app, seen = _seen_scope_app()
    status = _dispatcher_get(rs._PerUserSecretDispatcher(app), f"/mcp/{ledger['secret_a']}")
    assert status == 200
    assert seen["scope"]["path"] == "/mcp"  # 고정 마운트 경로로 바뀌어야 한다
    assert f"user={ledger['key_a']}" in seen["scope"]["query_string"].decode()


def test_dispatcher_overrides_client_supplied_user(ledger):
    """★핵심 회귀 방지: 요청자가 남의 이름표를 실어 보내도 열쇠 주인으로 덮인다.

    이 검사가 깨지면 namu-59가 없앤 결함(이름표만 바꿔 남의 서랍 열기)이
    그대로 되살아난다.
    """
    app, seen = _seen_scope_app()
    status = _dispatcher_get(
        rs._PerUserSecretDispatcher(app),
        f"/mcp/{ledger['secret_a']}",
        query=f"user={ledger['key_b']}&client=claude".encode(),
    )
    assert status == 200
    qs = seen["scope"]["query_string"].decode()
    assert f"user={ledger['key_a']}" in qs, "열쇠 주인으로 덮이지 않았다"
    assert ledger["key_b"] not in qs, "남의 이름표가 살아남았다"
    assert "client=claude" in qs, "다른 쿼리 항목까지 지워졌다"


def test_dispatcher_strips_every_duplicate_user_param(ledger):
    """`?user=A&user=B`처럼 여러 번 실어 보내는 수법 — 첫 항목만 고치면 뚫린다."""
    app, seen = _seen_scope_app()
    _dispatcher_get(
        rs._PerUserSecretDispatcher(app),
        f"/mcp/{ledger['secret_a']}",
        query=f"user={ledger['key_b']}&user={ledger['key_b']}".encode(),
    )
    qs = seen["scope"]["query_string"].decode()
    assert qs.count("user=") == 1
    assert ledger["key_b"] not in qs


def test_dispatcher_rejects_unknown_or_malformed_secret(ledger):
    app, _ = _seen_scope_app()
    dispatcher = rs._PerUserSecretDispatcher(app)
    for path in (
        "/mcp",                       # 열쇠 없음
        "/mcp/",                      # 빈 열쇠
        "/mcp/" + "z" * 43,           # 없는 열쇠
        "/mcp/short",                 # 형식 미달
        "/mcp/..",                    # 경로 조작
        f"/mcp/{ledger['secret_a']}/extra",   # 조각 2개
        f"/mcpx/{ledger['secret_a']}",        # 다른 경로
    ):
        assert _dispatcher_get(dispatcher, path) == 404, f"통과하면 안 되는 경로: {path}"


def test_dispatcher_fails_closed_when_ledger_unavailable(monkeypatch, ledger):
    """장부를 못 열면 통과시키지 않는다(열리는 방향으로 실패하면 전면 개방)."""
    def _boom(*args, **kwargs):
        raise RuntimeError("장부 열기 실패")

    monkeypatch.setattr(identity, "connect", _boom)
    app, _ = _seen_scope_app()
    status = _dispatcher_get(
        rs._PerUserSecretDispatcher(app), f"/mcp/{ledger['secret_a']}"
    )
    assert status == 404


def test_dispatcher_routes_each_secret_to_its_own_owner(ledger):
    """열쇠가 다르면 서랍도 달라야 한다(멀티테넌트 격리의 최종 확인)."""
    app, seen = _seen_scope_app()
    dispatcher = rs._PerUserSecretDispatcher(app)

    _dispatcher_get(dispatcher, f"/mcp/{ledger['secret_a']}")
    assert f"user={ledger['key_a']}" in seen["scope"]["query_string"].decode()

    _dispatcher_get(dispatcher, f"/mcp/{ledger['secret_b']}")
    assert f"user={ledger['key_b']}" in seen["scope"]["query_string"].decode()


# ---------------------------------------------------------------------------
# 첨부 파일 네 도구 (namu-file-upload-download 5·6단계)
#
# 파일이 실제로 GitHub과 오가는 부분은 tests/test_attach_files.py가 다룬다. 여기서
# 보는 것은 그 위층 배선이다: 첨부 기록이 함께 남는가 · 최신화 표시를 지우는가 ·
# 회원마다 갈리는가 · 받은 일은 기록하지 않는가.
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_github(monkeypatch):
    """GitHub 대신 쓰는 저장소 한 칸 — 경로 → 내용."""
    store: dict = {}

    def _upload(conn, key, name, content, message):
        path = rs.attach_files.normalize_name(name)
        replaced = path in store
        store[path] = content
        return {"path": path, "bytes": len(content), "replaced": replaced}

    def _download(conn, key, name):
        path = rs.attach_files.normalize_name(name)
        if path not in store:
            raise rs.attach_files.AttachError(f"저장소에 그런 파일이 없습니다: {path}")
        return store[path]

    def _delete(conn, key, name, message):
        path = rs.attach_files.normalize_name(name)
        if path not in store:
            raise rs.attach_files.AttachError(f"저장소에 그런 파일이 없습니다: {path}")
        del store[path]
        return path

    def _list(conn, key):
        return [{"path": p, "bytes": len(c)} for p, c in sorted(store.items())]

    monkeypatch.setattr(rs.attach_files, "upload", _upload)
    monkeypatch.setattr(rs.attach_files, "download", _download)
    monkeypatch.setattr(rs.attach_files, "delete", _delete)
    monkeypatch.setattr(rs.attach_files, "list_in_repo", _list)
    return store


def test_upload_stores_the_file_and_logs_it(fake_github, tmp_path):
    out = rs.namu_upload_file(
        name="설계.md", content_text="설계 원문 세 줄",
        summary="설계 문서", reason="파일째 남긴다", body="원문",
        topic="namu-70", project="proj-x", ctx=_ctx("alice"),
    )

    assert out["path"] == "attach_file/설계.md"
    assert out["bytes"] == len("설계 원문 세 줄".encode("utf-8"))
    assert out["status"] == "올림"
    assert fake_github["attach_file/설계.md"] == "설계 원문 세 줄".encode("utf-8")

    text = _yaml_text(tmp_path, "alice", "attachments.yaml")
    assert "attach_file/설계.md" in text
    assert "설계 문서" in text


def test_uploading_the_same_name_is_logged_as_a_revision(fake_github, tmp_path):
    rs.namu_upload_file(
        name="설계.md", content_text="1", summary="1판",
        reason="r", body="b", ctx=_ctx("alice"),
    )
    second = rs.namu_upload_file(
        name="설계.md", content_text="22", summary="2판",
        reason="r", body="b", ctx=_ctx("alice"),
    )

    assert second["status"] == "새 판"
    # 옛 기록이 남아야 "이 문서를 언제부터 몇 번 고쳤나"를 볼 수 있다.
    found = rs.namu_search(bowl="attachments", ctx=_ctx("alice"))
    assert [e["status"] for e in found["results"]] == ["새 판", "올림"]


def test_upload_requires_the_three_layers(fake_github):
    """파일 몸통은 각 PC로 안 내려오므로, 나중에 그 파일을 찾는 단서는 이 설명뿐이다."""
    with pytest.raises(ValueError) as exc:
        rs.namu_upload_file(
            name="설계.md", content_text="x", summary="  ",
            reason="r", body="b", ctx=_ctx("alice"),
        )
    assert "summary" in str(exc.value)
    assert fake_github == {}


def test_the_upload_tool_has_no_base64_field_at_all(fake_github):
    """칸이 있으면 붙은 AI가 그것을 쓴다(2026-08-07 실사용) — 그래서 없앴다.

    필수에서 선택으로 낮추고 설명에 "base64로 바꾸지 마세요"라고 굵게 적어 뒀는데도
    `.md` 파일 하나에 AI가 base64로 바꾸기 시작했고, 회원은 몇 분을 기다리다 응답을
    멈췄다. 다음에 "하위 호환을 위해 하나쯤"으로 되살아나는 것을 여기서 막는다.
    """
    import inspect

    params = inspect.signature(rs.namu_upload_file).parameters

    assert "content_base64" not in params
    # 글자 원문은 이제 선택이 아니라 필수다 — 내용 칸이 이것 하나뿐이다.
    assert params["content_text"].default is inspect.Parameter.empty


def test_upload_syncs_the_copy_in_the_same_call(fake_github, tmp_path):
    """파일을 올리면 GitHub에 이 서버가 모르는 커밋이 생긴다 — **이번 호출 안에서**
    맞춰야 한다.

    처음에는 설계서 6절대로 표시만 지워 다음 호출에 미뤘는데, 그러면 이번 호출의
    push가 반드시 거부된다(2026-08-07 첫 실사용에서 파일은 올라갔는데 올린 기록이
    저장소에 안 들어갔다). 맞췄다는 표시가 남아 있으면 미루지 않았다는 뜻이다.
    """
    seen = []
    real_ensure = rs.user_repo.ensure_ready

    def _counting_ensure(conn, key):
        seen.append(key)
        return real_ensure(conn, key)

    original = rs.user_repo.ensure_ready
    rs.user_repo.ensure_ready = _counting_ensure
    try:
        rs.namu_upload_file(
            name="설계.md", content_text="x", summary="s",
            reason="r", body="b", ctx=_ctx("alice"),
        )
    finally:
        rs.user_repo.ensure_ready = original

    # 올리기 뒤 최신화가 실제로 한 번 더 일어났다(첫 호출은 _sync_or_reject).
    assert len(seen) >= 2
    assert rs._sync_marker_path("alice").exists()


def test_list_merges_repo_names_with_the_log(fake_github):
    rs.namu_upload_file(
        name="설계.md", content_text="12345", summary="설계요약마커",
        reason="r", body="b", topic="namu-70", ctx=_ctx("alice"),
    )

    out = rs.namu_list_files(ctx=_ctx("alice"))

    assert out["count"] == 1
    row = out["files"][0]
    assert row["path"] == "attach_file/설계.md"
    assert row["bytes"] == 5
    assert row["summary"] == "설계요약마커"
    assert row["task"] == "namu-70"


def test_list_hides_removed_files_unless_asked(fake_github):
    rs.namu_upload_file(
        name="설계.md", content_text="x", summary="s",
        reason="r", body="b", ctx=_ctx("alice"),
    )
    rs.namu_delete_file(name="설계.md", reason="왜뺐는지마커", ctx=_ctx("alice"))

    assert rs.namu_list_files(ctx=_ctx("alice"))["count"] == 0

    with_removed = rs.namu_list_files(include_removed=True, ctx=_ctx("alice"))
    assert with_removed["count"] == 1
    assert with_removed["files"][0]["status"] == "지움"
    assert with_removed["files"][0]["reason"] == "왜뺐는지마커"


def test_download_returns_the_bytes_and_logs_nothing(fake_github):
    fake_github["attach_file/그림.bin"] = bytes(range(256))
    before = rs.namu_search(bowl="attachments", ctx=_ctx("alice"))["count"]

    out = rs.namu_download_file(
        name="그림.bin", force_base64=True, ctx=_ctx("alice")
    )

    import base64 as _b
    assert _b.b64decode(out["content_base64"]) == bytes(range(256))
    # 받은 일은 일부러 기록하지 않는다(2026-08-07 사용자 결정).
    assert rs.namu_search(bowl="attachments", ctx=_ctx("alice"))["count"] == before


# ---------------------------------------------------------------------------
# 글자 파일을 원문 그대로 주고받기(설계서 4절) — base64를 붙은 AI가 한 자씩
# 써야 해서 느렸던 문제를 없애는 경로.
# ---------------------------------------------------------------------------
def test_upload_accepts_plain_text_without_base64(fake_github, tmp_path):
    out = rs.namu_upload_file(
        name="메모.md", content_text="# 제목\n한글 본문", summary="s",
        reason="r", ctx=_ctx("alice"),
    )

    assert out["path"] == "attach_file/메모.md"
    assert fake_github["attach_file/메모.md"] == "# 제목\n한글 본문".encode("utf-8")
    assert out["bytes"] == len("# 제목\n한글 본문".encode("utf-8"))


def test_upload_rejects_empty_text(fake_github):
    with pytest.raises(ValueError, match="namu_create_upload_ticket"):
        rs.namu_upload_file(
            name="메모.md", content_text="   ", summary="s", reason="r",
            ctx=_ctx("alice"),
        )
    assert fake_github == {}


def test_upload_rejects_text_over_the_inline_limit(fake_github):
    too_big = "가" * (rs.attach_files.MAX_INLINE_TEXT_BYTES // 3 + 10)
    with pytest.raises(ValueError, match="namu_create_upload_ticket"):
        rs.namu_upload_file(
            name="큰글.md", content_text=too_big, summary="s", reason="r",
            ctx=_ctx("alice"),
        )
    assert fake_github == {}


def test_download_returns_text_files_as_plain_text(fake_github):
    rs.namu_upload_file(
        name="보고서.md", content_text="본문 한 줄", summary="s", reason="r",
        ctx=_ctx("alice"),
    )

    out = rs.namu_download_file(name="보고서.md", ctx=_ctx("alice"))

    assert out["content_text"] == "본문 한 줄"
    assert "content_base64" not in out
    assert "hint" not in out


def test_download_withholds_binary_and_points_at_the_ticket(fake_github):
    fake_github["attach_file/그림.bin"] = bytes(range(256))

    out = rs.namu_download_file(name="그림.bin", ctx=_ctx("alice"))

    # 칸은 있고 비어 있다 — 칸 자체가 없으면 붙은 AI가 실패로 읽고 되부른다.
    assert out["content_base64"] is None
    assert out["bytes"] == 256
    assert "namu_create_download_ticket" in out["hint"]


def test_download_withholds_text_that_is_too_large(fake_github):
    big = "a" * (rs.attach_files.MAX_INLINE_TEXT_BYTES + 1)
    fake_github["attach_file/큰글.md"] = big.encode("utf-8")

    out = rs.namu_download_file(name="큰글.md", ctx=_ctx("alice"))

    assert out["content_base64"] is None
    assert "hint" in out


def test_download_withholds_binary_wearing_a_text_extension(fake_github):
    """`.md` 이름을 단 바이너리 — 확장자만 믿으면 깨진 글자를 내주게 된다."""
    fake_github["attach_file/속임수.md"] = b"\xff\xfe\x00\x01"

    out = rs.namu_download_file(name="속임수.md", ctx=_ctx("alice"))

    assert out["content_base64"] is None
    assert "hint" in out


def test_download_of_a_missing_file_says_so(fake_github):
    with pytest.raises(rs.attach_files.AttachError, match="없습니다"):
        rs.namu_download_file(name="없는것.pdf", ctx=_ctx("alice"))


# ---------------------------------------------------------------------------
# 티켓 세 도구 + 티켓 주소(설계서 5·6·7절)
#
# 티켓 자체의 규칙(만료·1회용·번호 굵기)과 주소의 검증은 코어에서 시험한다
# (vendor/namu-agent의 test_tickets.py·test_ticket_web.py). 여기서 보는 것은
# **이 서버가 붙인 부분**이다 — 링크 주소를 짓는가, 회원마다 갈리는가, 발급이
# 저장소를 안 만지는가, 그리고 실제로 그 주소로 던진 파일이 회원 저장소와 첨부
# 기록에 자리 잡는가.
# ---------------------------------------------------------------------------
def _ticket_client():
    return TestClient(rs.build_ticket_app())


def test_creating_an_upload_ticket_touches_nothing_in_the_repository(fake_github):
    """안 쓰고 만료된 티켓이 저장소·기록 어디에도 흔적을 남기면 안 된다
    (설계서 12절)."""
    before = rs.namu_search(bowl="attachments", ctx=_ctx("alice"))["count"]

    out = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="발표 자료", reason="보관", ctx=_ctx("alice"),
    )

    assert out["upload_url"].startswith("https://namu-cloud.example/u/")
    assert out["name"] == "attach_file/발표.pptx"
    assert fake_github == {}
    assert rs.namu_search(bowl="attachments", ctx=_ctx("alice"))["count"] == before


def test_an_upload_ticket_checks_the_name_up_front(fake_github):
    """이름 검사를 미루면 회원이 파일을 다 올린 다음에야 튕기게 된다 — 그때는
    되돌릴 방법이 없다."""
    with pytest.raises(rs.attach_files.AttachError):
        rs.namu_create_upload_ticket(
            name="../밖으로.pptx", summary="s", reason="r", ctx=_ctx("alice"),
        )


def test_an_upload_ticket_needs_summary_and_reason(fake_github):
    with pytest.raises(ValueError, match="summary"):
        rs.namu_create_upload_ticket(
            name="발표.pptx", summary="  ", reason="r", ctx=_ctx("alice"),
        )


def test_a_file_posted_to_the_ticket_lands_in_the_repo_and_the_log(fake_github):
    """설계서 12절 — 티켓 주소로 올린 파일이 attach_file/에 들어가고
    namu_search(bowl='attachments')로 찾아진다."""
    ticket = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="발표자료마커", reason="보관",
        topic="namu-70", project="proj-x", ctx=_ctx("alice"),
    )

    r = _ticket_client().post(
        f"/u/{ticket['ticket_id']}",
        files={"file": ("아무이름.pptx", b"\x00\x01\x02\x03")},
        headers={"accept": "application/json"},
    )

    assert r.status_code == 200, r.text
    assert fake_github["attach_file/발표.pptx"] == b"\x00\x01\x02\x03"
    found = rs.namu_search(
        query="발표자료마커", bowl="attachments", ctx=_ctx("alice")
    )
    assert found["count"] == 1
    entry = found["results"][0]
    assert entry["path"] == "attach_file/발표.pptx"
    assert entry["bytes"] == 4
    # 발급할 때 AI가 적어 둔 작업·프로젝트가 그대로 따라와야 한다 — 회원이
    # 브라우저로 올릴 때 그 AI는 그 자리에 없다.
    assert entry["project"] == "proj-x"


def test_a_ticket_upload_may_read_the_ledger_while_storing(fake_github, monkeypatch):
    """저장 단계가 장부 커넥션을 **실제로 읽어도** 올리기가 성공해야 한다.

    운영에서 502로 드러난 결함의 자리다(2026-08-07 실측). 티켓 경로는 저장을
    다른 실행 흐름(threadpool)으로 넘기는데, 장부 커넥션은 요청을 받은 쪽에서
    열린다 — sqlite3는 만든 곳이 아닌 데서 쓰면 그 자리에서 거절한다. 이 파일의
    다른 티켓 시험들은 `ensure_ready` 대역이 커넥션을 건드리지 않아 통과했고,
    그래서 운영에서만 터졌다. 여기서는 대역이 운영처럼 커넥션을 읽는다.
    """
    def _reads_the_ledger(conn, key):
        conn.execute("SELECT 1").fetchone()
        (ur.user_dir(key) / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ur, "ensure_ready", _reads_the_ledger)
    monkeypatch.setattr(
        ur, "push",
        lambda conn, key, message=ur.DEFAULT_COMMIT_MESSAGE: (
            conn.execute("SELECT 1").fetchone() and False
        ),
    )

    ticket = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="s", reason="r", ctx=_ctx("alice"),
    )

    r = _ticket_client().post(
        f"/u/{ticket['ticket_id']}", files={"file": ("x.pptx", b"\x00\x01")},
        headers={"accept": "application/json"},
    )

    assert r.status_code == 200, r.text
    assert fake_github["attach_file/발표.pptx"] == b"\x00\x01"


def test_a_second_upload_of_the_same_name_is_logged_as_a_new_revision(fake_github):
    rs.namu_upload_file(
        name="발표.pptx", content_text="old", summary="s", reason="r",
        ctx=_ctx("alice"),
    )
    ticket = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="s2", reason="r2", ctx=_ctx("alice"),
    )

    r = _ticket_client().post(
        f"/u/{ticket['ticket_id']}", files={"file": ("x", b"new")},
        headers={"accept": "application/json"},
    )

    assert r.json()["status"] == "새 판"
    assert fake_github["attach_file/발표.pptx"] == b"new"


def test_the_ticket_page_opens_for_a_browser(fake_github):
    ticket = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="발표 자료", reason="보관", ctx=_ctx("alice"),
    )

    r = _ticket_client().get(
        f"/u/{ticket['ticket_id']}", headers={"accept": "text/html"}
    )

    assert r.status_code == 200
    assert "발표.pptx" in r.text
    # 이 서버의 화면 껍데기를 쓴다(개인 주소의 민무늬 화면이 아니라).
    assert "topbar" in r.text


def test_a_download_ticket_serves_the_file(fake_github):
    fake_github["attach_file/보고서.pdf"] = b"PDFBYTES"
    ticket = rs.namu_create_download_ticket(name="보고서.pdf", ctx=_ctx("alice"))

    r = _ticket_client().get(f"/d/{ticket['ticket_id']}")

    assert r.status_code == 200
    assert r.content == b"PDFBYTES"
    assert "attachment" in r.headers["content-disposition"]
    assert ticket["bytes"] == 8


def test_a_download_ticket_may_read_the_ledger_while_fetching(fake_github, monkeypatch):
    """받기도 올리기와 같은 자리에서 같은 이유로 502가 났다(2026-08-07).

    운영의 `fetch_file`은 "이 회원이 저장소를 연결했나"를 장부에서 읽고 시작한다.
    """
    fake_github["attach_file/보고서.pdf"] = b"PDFBYTES"
    real_download = rs.attach_files.download

    def _reads_the_ledger(conn, key, name):
        conn.execute("SELECT 1").fetchone()
        return real_download(conn, key, name)

    monkeypatch.setattr(rs.attach_files, "download", _reads_the_ledger)
    ticket = rs.namu_create_download_ticket(name="보고서.pdf", ctx=_ctx("alice"))

    r = _ticket_client().get(f"/d/{ticket['ticket_id']}")

    assert r.status_code == 200, r.text
    assert r.content == b"PDFBYTES"


def test_a_download_ticket_is_refused_for_a_file_that_is_not_there(fake_github):
    """회원이 눌렀을 때 비로소 깨지는 링크는 '받기가 고장났다'로 읽힌다."""
    with pytest.raises(ValueError, match="없습니다"):
        rs.namu_create_download_ticket(name="없는것.pdf", ctx=_ctx("alice"))


def test_check_ticket_reports_waiting_then_done(fake_github):
    ticket = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="s", reason="r", ctx=_ctx("alice"),
    )

    waiting = rs.namu_check_ticket(ticket["ticket_id"], ctx=_ctx("alice"))
    assert waiting["status"] == "대기중"

    _ticket_client().post(
        f"/u/{ticket['ticket_id']}", files={"file": ("x", b"ab")},
        headers={"accept": "application/json"},
    )

    done = rs.namu_check_ticket(ticket["ticket_id"], ctx=_ctx("alice"))
    assert done["status"] == "완료"
    assert done["path"] == "attach_file/발표.pptx"
    assert done["bytes"] == 2


def test_check_ticket_hides_another_members_ticket(fake_github):
    """있다고 알려 주면 번호를 넣어 보는 쪽에 정보를 주게 된다."""
    ticket = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="s", reason="r", ctx=_ctx("alice"),
    )

    out = rs.namu_check_ticket(ticket["ticket_id"], ctx=_ctx("bob"))

    assert out == {"status": "없음"}


def test_check_ticket_says_none_for_an_unknown_number(fake_github):
    assert rs.namu_check_ticket("그런것없음", ctx=_ctx("alice"))["status"] == "없음"


def test_a_logged_in_stranger_cannot_open_someone_elses_ticket(fake_github, monkeypatch):
    """설계서 12절 — 다른 사용자의 티켓 번호로 브라우저 접근하면 거절된다."""
    ticket = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="s", reason="r", ctx=_ctx("alice"),
    )
    monkeypatch.setattr(rs.web_auth, "_session_user_key", lambda request: "bob")

    r = _ticket_client().get(
        f"/u/{ticket['ticket_id']}", headers={"accept": "text/html"}
    )

    assert r.status_code == 403


def test_the_ticket_link_survives_a_failed_send(fake_github, monkeypatch):
    """설계서 7절 — AI의 curl이 막혀도 링크는 살아 있어야 회원이 브라우저로
    이어 올릴 수 있다."""
    ticket = rs.namu_create_upload_ticket(
        name="발표.pptx", summary="s", reason="r", ctx=_ctx("alice"),
    )

    def _boom(*a, **kw):
        raise rs.attach_files.AttachError("저장소가 대답하지 않음")

    monkeypatch.setattr(rs.attach_files, "upload", _boom)
    client = _ticket_client()
    failed = client.post(
        f"/u/{ticket['ticket_id']}", files={"file": ("x", b"ab")},
        headers={"accept": "application/json"},
    )
    assert failed.status_code == 502
    assert rs.namu_check_ticket(ticket["ticket_id"], ctx=_ctx("alice"))["status"] == "대기중"


def test_delete_removes_the_file_and_keeps_the_log(fake_github, tmp_path):
    rs.namu_upload_file(
        name="설계.md", content_text="12345", summary="설계요약마커",
        reason="r", body="b", ctx=_ctx("alice"),
    )

    out = rs.namu_delete_file(name="설계.md", reason="발표가 끝났다", ctx=_ctx("alice"))

    assert out["status"] == "지움"
    assert fake_github == {}
    text = _yaml_text(tmp_path, "alice", "attachments.yaml")
    assert "발표가 끝났다" in text
    # 크기는 마지막 기록에서 물려받는다 — 파일이 사라진 뒤에는 물어볼 곳이 없다.
    found = rs.namu_search(bowl="attachments", ctx=_ctx("alice"))
    assert found["results"][0]["bytes"] == 5


def test_delete_requires_a_reason(fake_github):
    rs.namu_upload_file(
        name="설계.md", content_text="x", summary="s",
        reason="r", body="b", ctx=_ctx("alice"),
    )
    with pytest.raises(ValueError, match="reason"):
        rs.namu_delete_file(name="설계.md", reason="   ", ctx=_ctx("alice"))
    # 거절했으면 파일은 그대로 있어야 한다.
    assert "attach_file/설계.md" in fake_github


def test_attachments_of_one_user_are_invisible_to_another(fake_github):
    rs.namu_upload_file(
        name="내파일.md", content_text="x", summary="s",
        reason="r", body="b", ctx=_ctx("alice"),
    )

    assert rs.namu_search(bowl="attachments", ctx=_ctx("bob"))["count"] == 0


# ---------------------------------------------------------------------------
# 첨부 변경 뒤 사본 최신화 순서 (2026-08-07 첫 실사용에서 드러난 결함)
#
# 파일을 GitHub에 올리면 저장소에 이 서버가 모르는 커밋이 하나 생긴다. 처음 구현은
# 설계서 6절대로 "최신화 표시만 지워 다음 호출이 맞추게" 했는데, 그러면 **이번
# 호출의 push가 반드시 거부된다** — 실제로 파일은 올라갔는데 올린 기록이 저장소에
# 안 들어갔고, 붙은 AI가 그 실패를 회원에게 그대로 전했다.
# ---------------------------------------------------------------------------


def test_upload_resyncs_the_copy_before_writing_the_log(fake_github, monkeypatch):
    """최신화(reset --hard)는 사본의 안 커밋된 변경을 지운다 — 기록을 먼저 쓰면
    그 기록이 날아간다. 그래서 최신화가 **기록보다 먼저** 와야 한다.

    최신화 대역이 첨부 기록 파일을 지우게 해 두고, 호출이 끝난 뒤에도 기록이 남아
    있는지로 순서를 확인한다(남아 있으면 기록이 최신화 뒤에 쓰인 것이다).
    """
    real_ensure = rs.user_repo.ensure_ready

    def _wiping_ensure(conn, key):
        target = rs._paths_for_user(key).attachments_yaml
        if target.exists():
            target.unlink()
        return real_ensure(conn, key)

    monkeypatch.setattr(rs.user_repo, "ensure_ready", _wiping_ensure)

    rs.namu_upload_file(
        name="설계.md", content_text="x", summary="설계요약마커",
        reason="r", body="b", ctx=_ctx("alice"),
    )

    found = rs.namu_search(bowl="attachments", ctx=_ctx("alice"))
    assert found["count"] == 1
    assert found["results"][0]["summary"] == "설계요약마커"


def test_upload_marks_the_copy_fresh_instead_of_leaving_it_stale(fake_github):
    """맞췄으면 '맞췄다'고 표시해야 한다 — 표시를 지워만 두면 다음 호출이 TTL과
    무관하게 또 통째로 최신화한다."""
    rs.namu_upload_file(
        name="설계.md", content_text="x", summary="s",
        reason="r", body="b", ctx=_ctx("alice"),
    )

    assert rs._sync_marker_path("alice").exists()


def test_upload_still_succeeds_when_the_resync_fails(fake_github, monkeypatch):
    """최신화가 실패해도 파일은 이미 GitHub에 올라갔다 — 여기서 도구를 실패로
    돌리면 회원이 같은 파일을 또 올린다. 대신 표시를 지워 다음 호출이 맞추게 한다."""
    calls = {"n": 0}

    def _failing_ensure(conn, key):
        calls["n"] += 1
        if calls["n"] > 1:  # 첫 호출(_sync_or_reject)은 통과시키고 그 뒤만 실패
            raise RuntimeError("최신화 실패(시험)")
        return rs.user_repo.user_dir(key)

    monkeypatch.setattr(rs.user_repo, "ensure_ready", _failing_ensure)
    # 사본이 없으면 _needs_sync가 항상 True라 첫 호출이 반드시 일어난다.
    rs.user_repo.user_dir("alice").mkdir(parents=True, exist_ok=True)

    out = rs.namu_upload_file(
        name="설계.md", content_text="x", summary="s",
        reason="r", body="b", ctx=_ctx("alice"),
    )

    assert out["path"] == "attach_file/설계.md"
    assert not rs._sync_marker_path("alice").exists()


def test_delete_also_resyncs_before_writing_the_log(fake_github, monkeypatch):
    rs.namu_upload_file(
        name="설계.md", content_text="12345", summary="s",
        reason="r", body="b", ctx=_ctx("alice"),
    )
    real_ensure = rs.user_repo.ensure_ready
    seen = []

    def _watching_ensure(conn, key):
        seen.append(rs._paths_for_user(key).attachments_yaml.exists())
        return real_ensure(conn, key)

    monkeypatch.setattr(rs.user_repo, "ensure_ready", _watching_ensure)

    rs.namu_delete_file(name="설계.md", reason="끝났다", ctx=_ctx("alice"))

    found = rs.namu_search(bowl="attachments", ctx=_ctx("alice"))
    assert [e["status"] for e in found["results"]] == ["지움", "올림"]
    assert rs._sync_marker_path("alice").exists()


def test_upload_does_not_require_body(fake_github, tmp_path):
    """body를 필수로 두면 안 된다(2026-08-07 첫 실사용).

    다른 기억처럼 필수로 뒀더니 붙은 AI가 그 칸을 빼고 불렀고, 거절당할 때마다
    **파일 내용 전체를 처음부터 다시 써서** 재시도했다 — 회원 눈에는 몇 분째 멈춘
    것으로 보였다. 첨부에서는 파일 자체가 원문이라 애초에 요구할 이유도 없다.
    """
    out = rs.namu_upload_file(
        name="설계.md", content_text="x", summary="설계요약마커",
        reason="파일째 남긴다", ctx=_ctx("alice"),
    )

    assert out["status"] == "올림"
    found = rs.namu_search(bowl="attachments", ctx=_ctx("alice"))
    assert found["results"][0]["body"] == "생략"


def test_upload_still_requires_summary_and_reason(fake_github):
    """이 둘은 남긴다 — 파일 몸통이 각 PC로 안 내려오므로 나중에 그 파일을 찾는
    단서가 이 두 줄뿐이다."""
    for missing in ("summary", "reason"):
        kwargs = {
            "name": "설계.md", "content_text": "x",
            "summary": "s", "reason": "r", "ctx": _ctx("alice"),
        }
        kwargs[missing] = "   "
        with pytest.raises(ValueError, match=missing):
            rs.namu_upload_file(**kwargs)


def test_upload_reports_how_long_each_step_took(fake_github):
    """서버 안에서 무엇이 오래 걸렸는지 밖에서 볼 방법이 없어 추측만 오갔다
    (2026-08-07) — 붙은 AI가 화면에 보여줄 수 있게 반환값에 싣는다."""
    out = rs.namu_upload_file(
        name="설계.md", content_text="x", summary="s",
        reason="r", ctx=_ctx("alice"),
    )

    assert set(out["seconds"]) == {
        "사본_최신화_전", "깃허브_올리기", "사본_최신화_후", "기록_저장",
        "기록_올리기", "합계",
    }
    assert all(isinstance(v, float) for v in out["seconds"].values())
