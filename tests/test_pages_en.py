"""영어 공개 페이지(pages_en.py) 유닛 테스트 — namu-83.

한국어판(`test_pages.py`)이 지키는 것을 영어판에도 그대로 건다. 그 위에 이
파일에만 있는 검사가 셋인데, 전부 **두 벌 관리**에서 실제로 나는 사고를
겨냥한다.

  1. 화면에 한국어 문장이 남지 않을 것 — 한국어판을 베껴 고치다 한 문단을
     빠뜨리면 영어 화면 한가운데에 한글이 남는다. 눈으로는 잘 안 보인다.
  2. 언어 단추가 왕복할 것 — 가서 돌아오지 못하면 방문자가 갇힌다.
  3. 로그인 뒤가 아직 한국어라고 미리 말할 것 — 이 사실을 숨기면 방문자가
     가입 도중에 막힌 채로 이유를 모른다.
"""
import re

import pytest

import pages
import pages_en
import ui

ALL_PAGES = list(pages_en.PAGES.items())

ALL_PUBLIC_PATHS = set(pages.PAGES) | set(pages_en.PAGES)
KNOWN_AUTH_PATHS = {"/auth/github/login", "/auth/me", "/auth/memory"}

_HANGUL = re.compile("[가-힣]")

# 언어 단추에 적히는 글자. 영어 화면에서 유일하게 한글이 허용되는 자리다 —
# 한국어판으로 건너가는 단추이므로 그 언어로 적히는 것이 맞다.
_ALLOWED_KOREAN = {"한국어"}


def _visible_text(html_doc: str) -> str:
    """화면에 실제로 보이는 글자만. 차림새(style)와 스크립트는 뺀다.

    이걸 안 빼면 `ui.SITE_CSS`와 말풍선 스크립트 안의 한국어가 통째로 걸려,
    한글 검사가 어느 화면에서든 실패한다.
    """
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html_doc, flags=re.S)
    return re.sub(r"<[^>]+>", " ", body)


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_every_english_page_renders_a_whole_document(path, render):
    out = render(False)

    assert out.startswith("<!doctype html>")
    assert '<html lang="en">' in out, "브라우저와 낭독기가 언어를 잘못 잡는다"
    assert "<title>" in out and "NAMU Cloud" in out
    assert 'name="viewport"' in out
    assert "prefers-color-scheme" in out


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_every_english_page_says_what_it_is_for_search_results(path, render):
    out = render(False)

    found = re.search(r'<meta name="description" content="([^"]+)"', out)
    assert found and len(found.group(1)) > 20


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_no_english_page_links_into_nowhere(path, render):
    out = render(False)

    for href in re.findall(r'href="(/[^"]*)"', out):
        target = href.split("?")[0].split("#")[0]
        assert target in ALL_PUBLIC_PATHS or target in KNOWN_AUTH_PATHS, (
            f"{path} 페이지가 없는 곳으로 보낸다: {href}"
        )


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_english_pages_carry_no_leftover_korean(path, render):
    """영어 화면에 한국어 문장이 남아 있으면 안 된다.

    한국어판을 고치고 이쪽을 잊는 것이 이 구조의 가장 흔한 사고인데, 그때 남는
    자국이 바로 이것이다. 언어 단추 하나만 예외다.
    """
    out = render(False)
    text = _visible_text(out)

    leftovers = [
        chunk
        for chunk in re.findall(r"[가-힣]+", text)
        if chunk not in _ALLOWED_KOREAN
    ]
    assert not leftovers, f"{path} 화면에 한국어가 남았다: {leftovers[:5]}"


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_english_pages_do_not_show_the_korean_only_assistant(path, render):
    """AI 안내원은 한국어로만 답한다(`ask.py`의 지시문 4번).

    영어 화면에 켜 두면 영어로 물어도 한국어 답이 돌아온다 — 읽지 못하는 답을
    주느니 단추를 내지 않는 편이 낫다. 그 지시문이 질문의 언어를 따라가도록
    바뀌면 이 시험과 `pages_en`의 `ask=False`를 함께 걷어내면 된다.
    """
    out = render(False)

    assert "namu-ask" not in out


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_english_pages_carry_no_member_information(path, render):
    out = render(False)

    assert "user key" not in out.lower()
    for shown in re.findall(r"/mcp/([A-Za-z0-9_-]{12,})", out):
        pytest.fail(f"진짜처럼 보이는 열쇠가 화면에 있다: {shown}")


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_english_pages_change_their_invitation_once_you_are_in(path, render):
    out_in = render(True)

    assert "/auth/me" in out_in


# ---------------------------------------------------------------------------
# 언어 사이를 오가는 길
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ko_path,en_path", sorted(ui.LANG_PAIRS.items()))
def test_the_language_button_makes_a_round_trip(ko_path, en_path):
    """가서 돌아올 수 있어야 한다. 한쪽만 열리면 방문자가 갇힌다."""
    ko_out = pages.PAGES[ko_path](False)
    en_out = pages_en.PAGES[en_path](False)

    assert f'<a class="btn langbtn" href="{en_path}"' in ko_out
    assert f'<a class="btn langbtn" href="{ko_path}"' in en_out


@pytest.mark.parametrize("ko_path,en_path", sorted(ui.LANG_PAIRS.items()))
def test_both_language_versions_point_search_engines_at_each_other(ko_path, en_path):
    """양쪽이 서로를 가리켜야 검색 엔진이 같은 화면의 두 언어판으로 인정한다.
    한쪽만 적으면 영어 화면이 별개의 얕은 페이지로 취급된다."""
    for out in (pages.PAGES[ko_path](False), pages_en.PAGES[en_path](False)):
        assert f'<link rel="alternate" hreflang="ko" href="{ko_path}">' in out
        assert f'<link rel="alternate" hreflang="en" href="{en_path}">' in out


def test_the_language_button_is_absent_where_there_is_no_translation():
    """로그인 뒤 화면에는 영어판이 없다. 갈 곳 없는 단추를 그리면 안 된다."""
    out = ui.page("내 페이지", "<p>x</p>", current="/auth/me")

    assert '<a class="btn langbtn"' not in out


def test_english_menu_and_pages_are_the_same_set():
    """메뉴에 있는데 안 열리는 항목도, 메뉴에 없는 떠돌이 페이지도 없어야 한다."""
    assert set(pages_en.PAGES) == {path for path, _label in ui.MENU_EN}


def test_language_pairs_cover_every_public_page():
    """짝이 빠진 화면이 있으면 그 화면에서만 언어 단추가 조용히 사라진다."""
    assert set(ui.LANG_PAIRS) == set(pages.PAGES)
    assert set(ui.LANG_PAIRS.values()) == set(pages_en.PAGES)


# ---------------------------------------------------------------------------
# 아직 번역되지 않은 곳을 미리 알리기
# ---------------------------------------------------------------------------
def test_start_page_warns_that_the_pages_after_sign_in_are_korean():
    """가입 도중에 화면이 한국어로 바뀌는 것을 미리 말하지 않으면, 방문자는
    막힌 뒤에야 그 사실을 알게 된다 — 그 자리에서 되돌아간다."""
    out = pages_en.start_page(False)

    warn_at = out.find("still Korean only")
    first_step_at = out.find("Sign in with GitHub")
    assert 0 < warn_at < first_step_at


def test_links_to_korean_only_documents_say_so_in_their_label():
    """바깥 안내서는 전부 한국어뿐이다. 이름표에 적지 않으면 눌러 들어간
    뒤에야 알게 된다.

    머리줄의 이름표만 `(KR)`로 줄여 적는다 — `(Korean)`을 다 적으면 메뉴가
    넘쳐 잘린다. 자리가 넉넉한 본문·꼬리말에서는 `(Korean)`을 그대로 쓴다.
    """
    for path, render in ALL_PAGES:
        out = render(False)
        for label in re.findall(
            r'href="https://onmiso-hash\.github\.io[^"]*"[^>]*>([^<]+)<', out
        ):
            assert "Korean" in label or "(KR)" in label, (
                f"{path}: 한국어 문서인데 이름표가 {label!r}"
            )


def _menu_width(menu, extra_labels=()) -> int:
    """머리줄 이름표가 차지하는 폭의 어림값 — 한글 한 자를 영문 두 자로 친다.

    픽셀을 재는 것이 아니라 **한국어판을 기준선으로 삼아 견주기 위한** 값이다.
    글꼴에 따라 실제 폭은 달라지므로 절대 기준으로 쓰면 안 되고, 지금 잘리지
    않는 것으로 확인된 한국어 메뉴보다 넓어지지 않는지만 본다.
    """
    labels = [label for _path, label in menu] + list(extra_labels)
    return sum(2 if "가" <= ch <= "힣" else 1 for label in labels for ch in label)


def test_the_english_menu_never_grows_wider_than_the_korean_one():
    """메뉴가 넘치면 가로로 밀리는데 스크롤 막대가 숨겨져 있어(`ui.py`의 `.menu`)
    **잘린 것처럼 보인다.** 2026-09-01에 실제로 그렇게 잘렸다.

    한국어 메뉴는 지금 폭에서 잘리지 않는 것이 확인된 기준선이다. 영어 쪽이
    그보다 넓어지지 않는 한 같은 사고는 다시 나지 않는다.
    """
    ko = _menu_width(ui.MENU, ["나무 안내서 ↗"])
    en = _menu_width(ui.MENU_EN, ["Guide (KR) ↗"])

    assert en <= ko, f"영어 메뉴가 한국어보다 넓다({en} > {ko}) — 잘려 보인다"


def test_english_faq_does_not_promise_prices_or_dates():
    """확인하지 않은 것을 적으면 그 순간 서비스의 약속이 된다(한국어판과 같다)."""
    out = pages_en.faq_page(False)

    assert "free" in out.lower()
    for phrase in ("free forever", "always free", "coming soon", "will be paid"):
        assert phrase not in out.lower()


def test_english_safety_page_keeps_the_notice_about_the_assistant():
    """영어 화면에는 말풍선이 없지만, 방문자는 한국어 화면으로 건너가 그것을 쓸
    수 있다. 고지는 양쪽에 그대로 있어야 한다."""
    out = pages_en.safety_page(False)

    assert "Google" in out
    assert "free tier" in out
    assert "no path to your memory" in out
