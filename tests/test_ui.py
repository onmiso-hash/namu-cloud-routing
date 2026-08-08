"""ui.py 유닛 테스트 — 사이트 공통 차림새(namu-70).

여기서 지키는 것은 "색이 예쁜가"가 아니라 **그 자리가 아니면 조용히 깨지는
약속들**이다: 껍데기 하나만 고치면 모든 화면에 반영된다는 전제, 알림 조각이
혼자 떨어져 나가도 색을 지고 있다는 것, 진행 표시가 눈으로 보지 않는 사용자에게도
읽힌다는 것.
"""
import re

import pytest

import ui


# ---------------------------------------------------------------------------
# 껍데기 — 모든 화면이 이 하나를 쓴다(web_auth._html_page가 그대로 넘긴다).
# ---------------------------------------------------------------------------
def test_shell_carries_the_mobile_and_dark_mode_guarantees():
    """이 세 가지가 빠지면 좁은 화면·어두운 화면에서만 티가 난다 — 만든 사람의
    화면에서는 멀쩡해 보여서 배포 뒤에야 발견되는 종류다."""
    out = ui.page("제목", "<p>본문</p>")

    assert 'name="viewport"' in out and "width=device-width" in out
    assert "prefers-color-scheme" in out
    # 공백 없는 긴 접속 주소가 좁은 화면을 옆으로 밀어내지 않게.
    assert "word-break" in out or "overflow-wrap" in out


def test_shell_escapes_the_title():
    out = ui.page('<script>x</script>', "<p>본문</p>")

    assert "<title>&lt;script&gt;" in out
    # 본문은 이미 만들어진 HTML이므로 그대로 실린다(호출부가 escape 책임).
    assert "<p>본문</p>" in out


def test_shell_pulls_nothing_from_another_server():
    """이 서비스는 기억을 다루는 화면이라 제3자 서버로 요청이 새는 경로를
    만들지 않는다 — 바깥 것을 끌어오는 태그의 주소는 전부 우리 것이거나
    페이지 안에 박힌 값(data:)이어야 한다."""
    out = ui.page("제목", "<p>본문</p>", reveal=True)

    assert "cdn" not in out.lower()
    for attr in re.findall(r'(?:href|src)="([^"]+)"', out):
        if attr.startswith(("http://", "https://")):
            # 바깥 주소는 사용자가 눌러야만 나가는 링크(<a>)에만 허용된다.
            assert f'<a href="{attr}"' in out, f"눌리지 않는 바깥 주소: {attr}"
    # 스크립트는 파일을 받아오지 않고 페이지 안에 통째로 들어 있어야 한다.
    for tag in re.findall(r"<script\b[^>]*>", out):
        assert "src=" not in tag, f"바깥 스크립트를 받아온다: {tag}"


def test_footer_leads_back_into_the_site_and_out_to_the_guides():
    """발은 두 몫을 한다 — 사이트 안을 계속 둘러보게 하는 것(같은 탭)과, 다른
    저장소가 띄운 문서로 내보내는 것(새 탭). 바깥 문서를 같은 탭에서 열면
    가입 도중이던 사람이 흐름 밖으로 튕겨 나간다."""
    out = ui.footer()

    links = re.findall(r'<a href="([^"]+)"([^>]*)>', out)
    inside = [(u, a) for u, a in links if u.startswith("/")]
    outside = [(u, a) for u, a in links if not u.startswith("/")]

    # 사이트 안 링크는 메뉴 전체를 담는다.
    assert {u for u, _a in inside} == {path for path, _label in ui.MENU}
    for _url, attrs in inside:
        assert "_blank" not in attrs
    assert outside, "바깥 안내서로 나가는 길이 하나도 없다"
    for url, attrs in outside:
        assert url.startswith("https://")
        assert 'target="_blank"' in attrs and "noopener" in attrs


def test_menu_marks_only_the_page_you_are_on():
    out = ui.topbar(current="/faq")

    assert out.count('class="on"') == 1
    assert '<a href="/faq" class="on">' in out


def test_menu_cta_switches_between_joining_and_coming_back():
    """가입 전에는 '시작하기', 로그인 뒤에는 '내 페이지'가 오른쪽 끝에 선다 —
    이미 가입한 사람에게 가입 버튼을 계속 내밀면 길을 잘못 든 느낌을 준다."""
    assert "/auth/github/login" in ui.topbar(cta="start")
    assert "/auth/me" in ui.topbar(cta="me")


# ---------------------------------------------------------------------------
# 알림 상자 — 혼자 떨어져 나가도 색을 지고 있어야 하는 조각.
# ---------------------------------------------------------------------------
def test_notice_carries_its_own_colour_and_is_announced():
    """연결 시험(namu-69)은 이 조각만 JSON으로 실어 보내 화면에 심는다. 색이
    클래스에 있으면 그 순간 아무 표시 없는 문단이 된다."""
    out = ui.notice("<b>됐습니다</b>", tone="good")

    assert 'role="status"' in out
    assert "background:" in out and "border-left:" in out
    assert "✅" in out


def test_notice_tones_are_actually_different():
    """네 가지 알림이 같은 색이면 나누는 의미가 없다."""
    seen = {ui.notice("x", tone=t) for t in ("info", "good", "warn", "bad")}

    assert len(seen) == 4


def test_notice_never_emits_two_style_attributes():
    """style이 두 개면 브라우저가 뒤엣것을 통째로 버려 색이 사라진다 — 진행
    표시 상자(display:none)가 정확히 이 함정을 밟는 자리다."""
    out = ui.notice("확인 중", tone="wait", attrs='id="x"', style_extra="display:none;")

    assert out.count("style=") == 1
    assert "display:none;" in out and "border-left:" in out
    assert 'id="x"' in out


# ---------------------------------------------------------------------------
# 진행 표시
# ---------------------------------------------------------------------------
def test_stepper_marks_exactly_the_steps_already_passed():
    out = ui.stepper(2, total=4)

    assert out.count('class="dot on"') == 2
    assert out.count('class="dot"') == 2


def test_stepper_is_also_readable_without_seeing_it():
    """동그라미는 화면 낭독기에 아무것도 읽히지 않는다 — 같은 뜻을 글자로도
    적고, 그림 쪽은 감춘다."""
    out = ui.stepper(2, total=4, label="기억 저장소")

    assert "2단계 / 4단계" in out
    assert "기억 저장소" in out
    assert 'aria-hidden="true"' in out


def test_stepper_escapes_the_label():
    assert "<b>" not in ui.stepper(1, label="<b>기억</b>")


# ---------------------------------------------------------------------------
# AI 안내원 말풍선 (namu-ai-guide 6단계)
#
# 여기서 지키는 것은 모양이 아니라 약속이다 — 열쇠가 없으면 아예 안 나타난다는
# 것과, 나타났다면 고지가 반드시 함께 있다는 것.
# ---------------------------------------------------------------------------
@pytest.fixture()
def ai_key_on(monkeypatch):
    """열쇠가 들어와 있는 상태(배포에서 환경변수 세 줄이 채워진 자리)."""
    monkeypatch.setenv("NAMU_ASK_API_KEY", "test-key")
    monkeypatch.setenv("NAMU_ASK_PROVIDER", "gemini")


def test_ask_button_is_not_drawn_without_a_key(monkeypatch):
    """열쇠가 없으면 눌러도 답이 안 오는 단추가 된다 — 그러느니 없는 편이 낫고,
    배포 순서가 어긋나도(코드가 먼저 나가도) 홈페이지가 지금과 똑같다."""
    monkeypatch.delenv("NAMU_ASK_API_KEY", raising=False)

    assert ui.ask_widget() == ""
    assert "namu-ask" not in ui.page("제목", "<p>본문</p>")


def test_ask_bubble_never_appears_without_the_notice(ai_key_on):
    """**이 설계에서 가장 지키고 싶은 한 줄이다.** 말풍선이 살아 있는데 고지가
    빠지면, 우리는 방문자에게 알리지 않고 그 글을 AI 회사로 넘기는 셈이 된다.
    문구를 손보는 것은 자유지만 '어디로 가는지·누가 볼 수 있는지·무엇을 적지
    말아야 하는지' 셋은 남아 있어야 한다."""
    out = ui.page("제목", "<p>본문</p>")

    assert "namu-ask" in out
    assert ui.ASK_NOTICE in out
    assert "Google" in ui.ASK_NOTICE and "학습" in ui.ASK_NOTICE
    assert "열쇠" in ui.ASK_NOTICE_STRONG
    # 더 자세한 설명으로 가는 문이 있어야 한 줄에 다 못 담은 것을 읽을 수 있다.
    assert 'href="/safety"' in out


def test_ask_bubble_can_be_left_off_a_screen(ai_key_on):
    assert "namu-ask" not in ui.page("제목", "<p>본문</p>", ask=False)


def test_ask_answer_goes_in_as_text_not_as_html(ai_key_on):
    """답은 우리가 만든 글이 아니라 AI가 만든 글이다. 그것을 HTML로 심으면
    답 한 줄로 화면을 갈아 끼울 수 있게 된다(설계서 10-2절)."""
    out = ui.page("제목", "<p>본문</p>")

    assert "textContent" in out
    assert "innerHTML" not in out
    assert "document.write" not in out


def test_ask_bubble_stops_key_like_text_before_it_leaves_the_browser(ai_key_on):
    """서버로 넘어간 뒤에 거르면 이미 늦다 — 그때는 벌써 AI 회사로 갈 길에 올라
    있다. 그래서 보내기 전에 화면에서 한 번 본다(설계서 10-2절 마지막 줄)."""
    out = ui.page("제목", "<p>본문</p>")

    assert "KEYLIKE" in out
    for prefix in ("gh", "github_pat_", "sk-", "AIza"):
        assert prefix in out


def test_ask_bubble_is_reachable_without_a_mouse_or_a_screen(ai_key_on):
    """동그란 단추에는 글자가 없다(그림 하나뿐이다) — 이름을 붙이지 않으면
    화면 낭독기에 아무것도 읽히지 않는다."""
    out = ui.ask_widget()

    assert "나무에게 물어보기" in out
    assert 'aria-expanded="false"' in out
    assert 'aria-live="polite"' in out
    assert 'aria-label="닫기"' in out


def test_ask_bubble_disappears_when_scripts_are_off(ai_key_on):
    """스크립트가 없으면 눌러도 아무 일이 안 일어난다 — 그런 단추는 안 보이는
    편이 낫다."""
    out = ui.ask_widget()

    assert "<noscript>" in out and "#namu-ask{display:none;}" in out


def test_ask_bubble_borrows_the_site_colours_instead_of_painting_its_own(ai_key_on):
    """색을 새로 박으면 어두운 화면에서 그 자리만 하얗게 남는다."""
    assert ".ask-panel{" in ui.SITE_CSS
    ask_css = ui.SITE_CSS[ui.SITE_CSS.index(".ask{position:fixed") :]

    assert "#" not in ask_css, "말풍선이 색을 직접 박았다 — var(--…)만 쓴다"


def test_ask_button_says_what_it_is_instead_of_leaving_a_lone_icon(ai_key_on):
    """**그림 하나만 두면 "이게 무슨 아이콘이지?"에서 멈춘다.**

    2026-08-08 사용자 실측 — 앞 판은 그림글자 💬 하나였는데, 그림글자는 기기의
    글꼴이 그리므로 휴대폰에서는 말풍선 안에 점 세 개가 보이고 웹에서는 안
    보였다. 같은 글자인데 한쪽에서만 "대화창"으로 읽힌 것이다.

    그래서 둘을 지킨다 — 그림은 우리가 직접 그리고(기기마다 달라지지 않는다),
    넓은 화면에는 글자도 함께 낸다.
    """
    out = ui.ask_widget()

    assert "💬" not in out, "그림글자에 맡기면 기기마다 다르게 그려진다"
    assert "<svg" in out and "circle" in out, "말풍선 그림을 직접 그려야 한다"
    assert ">물어보기<" in out, "넓은 화면에서 읽을 글자가 단추에 없다"
    # 좁은 화면에서는 글자를 접으므로, 그때도 이름이 읽히도록 남겨 둔다.
    assert 'aria-label="나무에게 물어보기"' in out
