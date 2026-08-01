"""공개 페이지(pages.py) 유닛 테스트 — 홈·시작하기·무엇을 기억하나·안전·질문.

이 페이지들은 **로그인하지 않은 낯선 사람이 가장 먼저 보는 화면**이다. 그래서
여기서 지키는 것은 문장의 맛이 아니라 다음 세 가지다.

  1. 눌러도 아무 데도 안 가는 링크가 없을 것 (첫인상에서 가장 티가 난다)
  2. 로그인하지 않은 사람의 화면에 회원 정보가 한 글자도 없을 것
  3. 이미 가입한 사람에게 가입 버튼만 내밀지 않을 것
"""
import re

import pytest

import pages
import ui

ALL_PAGES = list(pages.PAGES.items())

# 사이트가 걸어도 되는, 공개 목록 밖의 우리 주소. 로그인 왕복과 로그인 뒤
# 화면들이라 여기 적어 둔다 — 이 목록에 없는 새 주소를 페이지가 걸면
# 아래 시험이 먼저 실패해, 아직 없는 화면으로 사람을 보내는 일이 없다.
KNOWN_AUTH_PATHS = {"/auth/github/login", "/auth/me", "/auth/memory"}


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_every_public_page_renders_a_whole_document(path, render):
    out = render(False)

    assert out.startswith("<!doctype html>")
    assert "<title>" in out and "나무 클라우드" in out
    assert 'name="viewport"' in out
    assert "prefers-color-scheme" in out


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_every_public_page_says_what_it_is_for_search_results(path, render):
    """검색 결과·메신저 미리보기에 뜨는 한 줄이다. 없으면 주소만 덩그러니
    뜨는데, 처음 보는 사람에게는 그게 곧 '수상한 링크'다."""
    out = render(False)

    found = re.search(r'<meta name="description" content="([^"]+)"', out)
    assert found and len(found.group(1)) > 20


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_no_public_page_links_into_nowhere(path, render):
    """사이트 안을 가리키는 링크는 실제로 열리는 곳이어야 한다."""
    out = render(False)

    for href in re.findall(r'href="(/[^"]*)"', out):
        target = href.split("?")[0].split("#")[0]
        assert target in pages.PAGES or target in KNOWN_AUTH_PATHS, (
            f"{path} 페이지가 없는 곳으로 보낸다: {href}"
        )


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_public_pages_carry_no_member_information(path, render):
    """로그인 전 화면이라 보여 줄 회원 정보 자체가 없어야 한다. 예시 주소를
    적을 때도 진짜처럼 보이는 열쇠를 지어내지 않는다."""
    out = render(False)

    assert "사용자 키" not in out
    assert "연결된 저장소" not in out
    # 접속 주소의 생김새를 보여주는 자리에는 사람이 읽는 자리표시자가 있어야 한다.
    for shown in re.findall(r"/mcp/([A-Za-z0-9_-]{12,})", out):
        pytest.fail(f"진짜처럼 보이는 열쇠가 화면에 있다: {shown}")


@pytest.mark.parametrize("path,render", ALL_PAGES)
def test_pages_change_their_invitation_once_you_are_in(path, render):
    """가입한 사람에게 계속 '시작하기'만 내밀면 길을 잘못 든 느낌을 준다."""
    out_in = render(True)

    assert "/auth/me" in out_in


def test_home_puts_the_fork_before_the_steps():
    """터미널 사용자를 먼저 갈라 보내지 않으면, 반쪽짜리로 설치하고 기억이
    두 곳으로 흩어진 뒤에야 알게 된다 — 그때는 되돌리기 어렵다."""
    out = pages.home_page(False)

    fork_at = out.find("터미널에서 AI를 쓴다")
    steps_at = out.find("받은 주소를 AI에 붙이기")
    assert 0 < fork_at < steps_at


def test_home_tells_where_the_memory_actually_lives():
    """이 서비스에 대해 사람들이 가장 먼저 하는 걱정이다 — 첫 화면에서
    답하지 않으면 아래로 내려가지 않는다."""
    out = pages.home_page(False)

    assert "내 GitHub 저장소" in out
    assert "사본" in out


def test_start_page_warns_the_terminal_user_before_the_first_step():
    out = pages.start_page(False)

    warn_at = out.find("터미널에서 AI를 쓰신다면")
    first_step_at = out.find("GitHub으로 로그인합니다")
    assert 0 < warn_at < first_step_at


def test_start_page_does_not_promise_the_site_creates_the_repository():
    """저장소는 사이트가 대신 만들지 않는다(권한 문구 대가 때문에 버린 길).
    화면이 만들어 주는 것처럼 적으면 그 자리에서 흐름이 끊긴다."""
    out = pages.start_page(False)

    assert pages.NEW_REPO_URL in out
    assert "GitHub에서 만들기" in out


def test_memory_page_does_not_claim_the_web_can_write_the_work_log():
    """작업일지는 이 주소로 남길 수 없다 — '무엇이든 기억한다'고 적으면
    사용자가 되지 않는 일을 시키게 된다."""
    out = pages.memory_page(False)

    assert "작업일지" in out
    assert "읽는 것" in out


def test_safety_page_states_the_three_verdicts_of_the_connection_test():
    """'지금은 확인 불가'를 빼면 사용자가 멀쩡한 주소를 스스로 폐기한다."""
    out = pages.safety_page(False)

    for verdict in ("살아있음", "주소가 잘못됨", "지금은 확인 불가"):
        assert verdict in out


def test_faq_does_not_promise_prices_or_dates():
    """확인하지 않은 것을 적으면 그 순간 서비스의 약속이 된다."""
    out = pages.faq_page(False)

    assert "요금제는 아직 정해진 것이 없" in out
    for word in ("평생 무료", "영원히 무료", "곧 출시"):
        assert word not in out


def test_menu_and_pages_are_the_same_set():
    """메뉴에 있는데 안 열리는 항목도, 메뉴에 없는 떠돌이 페이지도 없어야 한다."""
    assert set(pages.PAGES) == {path for path, _label in ui.MENU}
