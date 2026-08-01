"""ui.py 유닛 테스트 — 사이트 공통 차림새(namu-70).

여기서 지키는 것은 "색이 예쁜가"가 아니라 **그 자리가 아니면 조용히 깨지는
약속들**이다: 껍데기 하나만 고치면 모든 화면에 반영된다는 전제, 알림 조각이
혼자 떨어져 나가도 색을 지고 있다는 것, 진행 표시가 눈으로 보지 않는 사용자에게도
읽힌다는 것.
"""
import re

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
