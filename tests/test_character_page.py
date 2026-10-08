"""캐릭터 만들기 공개 화면(`/character`) — 나무 캐릭터 설계서 0·11장.

여기서 지키는 것:
  1. 로그인 없이 열린다(문 목록과 라우트 양쪽).
  2. 질문은 화면에 손으로 적지 않고 코어 `character.schema()`에서 온다.
  3. 천장 규칙의 숫자도 코어 상수에서 온다.
  4. 사이트 밖으로 요청이 새지 않는다(프로토타입은 구글 글꼴을 불렀다).
  5. 영어판(`/en/character`)은 같은 틀에 보이는 글자만 바꾼다 — 코어에 질문이
     늘었는데 번역이 빠지면 여기서 걸린다.
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


# ---------------------------------------------------------------------------
# 위쪽 메뉴와 영어판
# ---------------------------------------------------------------------------
_HANGUL = re.compile(r"[가-힣]")


def test_character_is_in_both_menus_and_the_language_button_pairs_them():
    ko = character_page.character_page(False)
    en = character_page.character_page_en(False)

    assert '<a href="/character"' in ko and '<a href="/en/character"' in en
    assert ui.LANG_PAIRS["/character"] == "/en/character"
    assert 'href="/en/character"' in ko  # 한국어 화면의 언어 단추
    assert 'href="/character"' in en  # 영어 화면의 언어 단추
    assert '<html lang="en"' in en


def test_english_page_opens_without_login():
    client = TestClient(wa.build_auth_app(), base_url="https://testserver")

    r = client.get("/en/character")

    assert r.status_code == 200
    assert "/en/character" in rs._WEB_PATHS
    assert len(_page_data(r.text)["questions"]) == 10


def test_english_questions_keep_every_value_and_limit_of_the_core():
    """번역은 보이는 글자만 바꾼다. 칸 이름·고르기 값·상한이 달라지면 영어판에서
    만든 카드가 저장 검사에서 엉뚱하게 거절된다."""
    import character

    core = character.schema()["questions"]
    en = _page_data(character_page.character_page_en(False))["questions"]

    assert [q["key"] for q in en] == [q["key"] for q in core]
    shown = {"label", "title", "hint", "custom_label", "options"}
    for c, e in zip(core, en):
        assert {k: v for k, v in c.items() if k not in shown} == {
            k: v for k, v in e.items() if k not in shown
        }, c["key"]
        assert len(e["options"]) == len(c["options"]), c["key"]
        if c["type"] == "choice":
            assert [o["value"] for o in e["options"]] == [o["value"] for o in c["options"]]


def test_english_page_shows_no_korean_text():
    """질문·약속·스크립트가 쓰는 문구에 한국어가 남으면 영어 방문자에게 그대로 보인다.
    스크립트 안의 설명 주석과 언어 단추(`한국어`)는 보이는 글자가 아니므로 뺀다."""
    out = character_page.character_page_en(False)
    data = _page_data(out)

    assert not _HANGUL.search(json.dumps(data, ensure_ascii=False))
    assert not _HANGUL.search(json.dumps(character_page._TEXT["en"], ensure_ascii=False))
    visible = re.sub(r"<script.*?</script>|<style.*?</style>", "", out, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible).replace("한국어", "")
    assert not _HANGUL.search(visible), _HANGUL.search(visible)


def test_both_languages_have_the_same_screen_text_keys():
    ko, en = character_page._TEXT["ko"], character_page._TEXT["en"]

    assert set(ko) == set(en)
    assert set(ko["js"]) == set(en["js"])


def test_english_save_errors_cover_every_error_code_of_the_save_route():
    """영어판은 서버의 한국어 문장 대신 오류 부호로 문구를 고른다. 저장 주소에
    부호가 늘었는데 여기 없으면 영어 방문자는 뭉뚱그린 문구만 본다."""
    import inspect

    codes = set(re.findall(r'"error": "(\w+)"', inspect.getsource(wa._character_save_sync)))
    codes.discard("invalid_card")  # 따로 다룬다(`invalid_card` 문구)

    assert codes == set(character_page._TEXT["en"]["js"]["errors"])
