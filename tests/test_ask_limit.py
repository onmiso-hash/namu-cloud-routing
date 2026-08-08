"""ask_limit.py 유닛 테스트 — AI 안내원 하루 한도.

여기서 지키는 것은 **틀리면 조용히 무너지는 약속들**이다. 한도는 평소에 아무
일도 하지 않다가 사고가 났을 때만 일하는 장치라, 깨져 있어도 티가 안 난다.

- 컨테이너가 다시 떠도 세던 값이 남는가 (메모리에만 세면 다시 뜨는 순간 0)
- 자정에 정확히 초기화되는가 (한국 시각 기준)
- 쿠키를 지운 사람을 접속 주소로 잡는가
- **AI를 부르기 전에 세는가** (부른 뒤에 세면 실패를 되풀이해 무한히 우회한다)
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import ask_limit
import pytest

SEOUL = ZoneInfo("Asia/Seoul")


@pytest.fixture
def limiter(tmp_path):
    return ask_limit.Limiter(tmp_path / "ask_counters.json", per_person=3, daily_cap=5)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=SEOUL)


# ---------------------------------------------------------------------------
# 사람별 한도
# ---------------------------------------------------------------------------
def test_person_limit_blocks_after_its_quota(limiter):
    for _ in range(3):
        assert limiter.check_and_count("쿠키A", "1.1.1.1").allowed

    blocked = limiter.check_and_count("쿠키A", "1.1.1.1")
    assert not blocked.allowed
    assert blocked.reason == "person"
    assert "내일" in blocked.message


def test_a_second_person_is_unaffected(limiter):
    for _ in range(3):
        limiter.check_and_count("쿠키A", "1.1.1.1")

    assert limiter.check_and_count("쿠키B", "2.2.2.2").allowed


def test_clearing_the_cookie_does_not_reset_the_person(limiter):
    """쿠키는 지울 수 있다. 접속 주소가 두 번째 겹이다."""
    for _ in range(3):
        limiter.check_and_count("쿠키A", "1.1.1.1")

    blocked = limiter.check_and_count("", "1.1.1.1")
    assert not blocked.allowed and blocked.reason == "person"


def test_changing_the_ip_does_not_reset_the_person(limiter):
    """반대 방향 — 접속 주소를 바꿔도 쿠키가 그대로면 잡힌다."""
    for _ in range(3):
        limiter.check_and_count("쿠키A", "1.1.1.1")

    blocked = limiter.check_and_count("쿠키A", "9.9.9.9")
    assert not blocked.allowed and blocked.reason == "person"


# ---------------------------------------------------------------------------
# 사이트 전체 한도
# ---------------------------------------------------------------------------
def test_site_limit_blocks_everyone(tmp_path):
    limiter = ask_limit.Limiter(tmp_path / "c.json", per_person=100, daily_cap=3)
    for i in range(3):
        assert limiter.check_and_count(f"쿠키{i}", f"1.1.1.{i}").allowed

    blocked = limiter.check_and_count("새사람", "5.5.5.5")
    assert not blocked.allowed
    assert blocked.reason == "total"


def test_site_limit_outranks_the_person_limit(tmp_path):
    """전체가 찼으면 아직 여유 있는 사람도 막힌다 — 그게 마지막 방어선이다."""
    limiter = ask_limit.Limiter(tmp_path / "c.json", per_person=100, daily_cap=1)
    limiter.check_and_count("쿠키A", "1.1.1.1")

    assert limiter.check_and_count("쿠키A", "1.1.1.1").reason == "total"


# ---------------------------------------------------------------------------
# 자정 초기화
# ---------------------------------------------------------------------------
def test_counters_reset_at_korean_midnight(limiter):
    for _ in range(3):
        limiter.check_and_count("쿠키A", "1.1.1.1", now=at("2026-08-08 23:59"))
    assert not limiter.check_and_count("쿠키A", "1.1.1.1", now=at("2026-08-08 23:59")).allowed

    assert limiter.check_and_count("쿠키A", "1.1.1.1", now=at("2026-08-09 00:01")).allowed


def test_the_day_is_korean_not_utc(monkeypatch):
    """컨테이너는 UTC로 돈다. 시간대를 놓치면 한국 오전 9시에 초기화된다."""
    monkeypatch.delenv("NAMU_TZ", raising=False)
    utc_evening = datetime(2026, 8, 8, 16, 30, tzinfo=ZoneInfo("UTC"))

    assert ask_limit.today(utc_evening) == "2026-08-09"


# ---------------------------------------------------------------------------
# 다시 떠도 남는가
# ---------------------------------------------------------------------------
def test_counts_survive_a_restart(tmp_path):
    path = tmp_path / "c.json"
    first = ask_limit.Limiter(path, per_person=2, daily_cap=99)
    first.check_and_count("쿠키A", "1.1.1.1")
    first.check_and_count("쿠키A", "1.1.1.1")

    # 컨테이너가 다시 뜬 상황 — 같은 파일을 보는 새 객체.
    second = ask_limit.Limiter(path, per_person=2, daily_cap=99)
    assert not second.check_and_count("쿠키A", "1.1.1.1").allowed


def test_a_broken_counter_file_does_not_crash_the_site(tmp_path):
    """세기 파일이 깨졌다고 홈페이지가 죽으면 안 된다 — 0부터 다시 센다."""
    path = tmp_path / "c.json"
    path.write_text("{망가진 파일", encoding="utf-8")
    limiter = ask_limit.Limiter(path, per_person=2, daily_cap=99)

    assert limiter.check_and_count("쿠키A", "1.1.1.1").allowed


def test_no_half_written_file_is_left_behind(tmp_path):
    """임시 파일에 쓰고 이름을 바꾸므로, 끝난 뒤 임시 파일이 남지 않는다."""
    path = tmp_path / "c.json"
    limiter = ask_limit.Limiter(path, per_person=9, daily_cap=99)
    limiter.check_and_count("쿠키A", "1.1.1.1")

    assert path.exists()
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------------------
# 세는 시점과 개인정보
# ---------------------------------------------------------------------------
def test_peek_does_not_count(limiter):
    """화면을 그릴 때 남은 횟수를 보는 용도 — 이게 세면 열어만 봐도 한도가 준다."""
    for _ in range(5):
        limiter.peek("쿠키A", "1.1.1.1")

    assert limiter.check_and_count("쿠키A", "1.1.1.1").allowed


def test_blocked_attempts_are_not_counted(limiter):
    """막힌 요청까지 세면 파일이 끝없이 커진다."""
    for _ in range(3):
        limiter.check_and_count("쿠키A", "1.1.1.1")
    for _ in range(10):
        limiter.check_and_count("쿠키A", "1.1.1.1")

    import json

    data = json.loads(limiter.path.read_text(encoding="utf-8"))
    assert data["total"] == 3


def test_raw_ip_is_never_written_to_disk(limiter):
    """접속 주소는 개인정보다. 세기에 필요한 것은 '같은 사람인가'뿐이다."""
    limiter.check_and_count("쿠키A", "203.0.113.45")

    saved = limiter.path.read_text(encoding="utf-8")
    assert "203.0.113.45" not in saved
    assert "쿠키A" not in saved


def test_fingerprint_is_salted(monkeypatch):
    """소금이 없으면 IPv4 42억 개를 전부 만들어 보고 원래 주소를 되찾을 수 있다."""
    monkeypatch.setenv("NAMU_SESSION_SECRET", "소금하나")
    one = ask_limit.fingerprint("203.0.113.45")
    monkeypatch.setenv("NAMU_SESSION_SECRET", "소금둘")

    assert ask_limit.fingerprint("203.0.113.45") != one


def test_remaining_counts_down(limiter):
    assert limiter.check_and_count("쿠키A", "1.1.1.1").remaining == 2
    assert limiter.check_and_count("쿠키A", "1.1.1.1").remaining == 1
    assert limiter.check_and_count("쿠키A", "1.1.1.1").remaining == 0


# ---------------------------------------------------------------------------
# 설정값
# ---------------------------------------------------------------------------
def test_limits_come_from_the_environment(tmp_path, monkeypatch):
    """무료 등급의 실제 하루 한도를 아직 모른다 — 알게 되면 다시 배포하지 않고
    설정만 고쳐 넣을 수 있어야 한다(설계서 7-1절)."""
    monkeypatch.setenv("NAMU_ASK_PER_PERSON", "1")
    monkeypatch.setenv("NAMU_ASK_DAILY_CAP", "7")
    limiter = ask_limit.Limiter(tmp_path / "c.json")

    assert (limiter.per_person, limiter.daily_cap) == (1, 7)


def test_a_nonsense_setting_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("NAMU_ASK_DAILY_CAP", "많이")
    limiter = ask_limit.Limiter(tmp_path / "c.json")

    assert limiter.daily_cap == ask_limit._DEFAULT_DAILY_CAP
