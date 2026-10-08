"""캐릭터 만들기 공개 화면(`/character`) — 나무 캐릭터 설계서 0·11장.

여기서 지키는 것:
  1. 로그인 없이 열린다(문 목록과 라우트 양쪽).
  2. 질문은 화면에 손으로 적지 않고 코어 `character.schema()`에서 온다.
  3. 천장 규칙의 숫자도 코어 상수에서 온다.
  4. 사이트 밖으로 요청이 새지 않는다(프로토타입은 구글 글꼴을 불렀다).
"""
import json
import re

import pytest
from starlette.testclient import TestClient

import character_page
import routing_server as rs  # noqa: F401 — vendor/namu-plugin을 sys.path에 얹는다
import ui
import web_auth as wa


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("NAMU_SESSION_SECRET", "test-session-secret-value")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_ID", "Iv1.testclientid")
    monkeypatch.setenv("NAMU_GITHUB_CLIENT_SECRET", "test-client-secret-marker-9f2a")
    monkeypatch.setenv("NAMU_IDENTITY_DB_PATH", str(tmp_path / "identity.db"))
    yield


def _page_data(out: str) -> dict:
    found = re.search(
        r'<script type="application/json" id="cm-schema">(.*?)</script>', out, re.S
    )
    assert found, "질문 데이터가 화면에 실려 있지 않다"
    return json.loads(found.group(1))


def test_character_page_opens_without_login_and_carries_ten_questions():
    client = TestClient(wa.build_auth_app(), base_url="https://testserver")

    r = client.get("/character")

    assert r.status_code == 200
    data = _page_data(r.text)
    assert len(data["questions"]) == 10


def test_character_path_is_a_public_door():
    assert "/character" in ui.PUBLIC_PATHS
    assert "/character" in rs._WEB_PATHS


def test_questions_and_rules_come_from_the_core():
    import character

    data = _page_data(character_page.character_page(False))

    assert data["questions"] == character.schema()["questions"]
    assert data["promises"] == list(character.PROMISES)
    assert data["ceiling_rank"] == {c["value"]: c["rank"] for c in character.CEILINGS}
    assert data["start_min_ceiling_rank"] == character.START_MIN_CEILING_RANK
    assert set(data["card_keys"]) == set(character.CARD_KEYS)
    # 카드 칸 순서는 설계서 5.1(스키마 예시)과 같다.
    assert data["card_keys"] == list(character.schema()["example"])


def test_page_has_no_hand_written_question_list_or_outside_fonts():
    out = character_page.character_page(False)

    for leftover in ("START_CODE", "CEIL_CODE", "STEPS", "Gowun", "googleapis", "gstatic"):
        assert leftover not in out
    assert "</script>" not in _page_data(out).__repr__()


def test_page_links_only_to_known_places():
    import pages
    import pages_en

    known = set(pages.PAGES) | set(pages_en.PAGES) | set(character_page.PAGES)
    known |= {"/auth/github/login", "/auth/me"} | set(ui.ICON_PATHS)
    out = character_page.character_page(False)

    for href in re.findall(r'href="(/[^"]*)"', out):
        assert href.split("?")[0].split("#")[0] in known, href
