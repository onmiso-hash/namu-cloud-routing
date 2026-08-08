"""ask.py 유닛 테스트 — 안내원이 질문 하나를 답 하나로 바꾸는 길.

여기서 지키는 것은 "답이 좋은가"가 아니다. 답의 품질은 모델이 정하고 우리가
검사할 수 없다. 대신 **틀리면 방문자가 속거나 서비스가 무너지는 자리**를 지킨다.

- 열쇠가 없으면 아무 일도 안 하는가 (없는데 부르면 배포 순서가 사고가 된다)
- 말뭉치에서 못 찾으면 **AI를 안 부르는가** (부르면 지어낼 여지가 생긴다)
- 근거 링크가 **우리 목록에서만** 나오는가 (모델이 주소를 지어낼 길)
- AI가 어떻게 터져도 예외가 밖으로 안 나가는가 (홈페이지가 함께 죽는다)
- 방문자 글이 안내문 자리로 새지 않는가 ("지시 심기")

네트워크는 한 줄도 타지 않는다. `ask._PROVIDERS`의 호출 함수 하나만 갈아 끼우면
나머지 전부를 확인할 수 있게 만들어 둔 것이 이 검사의 전제다.
"""
import ask
import ask_corpus
import ask_limit
import pytest


# ---------------------------------------------------------------------------
# 도우미 — 네트워크 대신 가짜 회사 하나
# ---------------------------------------------------------------------------
class FakeProvider:
    """부른 것을 그대로 기록해 두는 가짜 AI."""

    def __init__(self, reply: str = "무료입니다.\n근거: [1]"):
        self.reply = reply
        self.calls: "list[dict]" = []

    def __call__(self, system: str, user: str, *, model: str, key: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model, "key": key})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    """모든 검사는 열쇠가 있는 상태에서 시작한다(없는 경우는 따로 검사한다)."""
    monkeypatch.setenv("NAMU_ASK_API_KEY", "테스트열쇠")
    monkeypatch.setenv("NAMU_ASK_PROVIDER", "gemini")
    monkeypatch.setenv("NAMU_ASK_MODEL", "테스트모델")


@pytest.fixture(scope="module")
def corpus():
    return ask_corpus.Corpus.load()


@pytest.fixture
def make_guide(tmp_path, corpus, monkeypatch):
    def _make(reply: "str | Exception" = "무료입니다.\n근거: [1]", **limits):
        fake = FakeProvider(reply)
        monkeypatch.setitem(ask._PROVIDERS, "gemini", fake)
        limiter = ask_limit.Limiter(
            tmp_path / "ask_counters.json",
            per_person=limits.get("per_person", 10),
            daily_cap=limits.get("daily_cap", 800),
        )
        return ask.Guide(corpus, limiter), fake

    return _make


# ---------------------------------------------------------------------------
# 열쇠가 없을 때 — 배포 순서가 어긋나도 사고가 나지 않아야 한다
# ---------------------------------------------------------------------------
def test_disabled_without_a_key(monkeypatch):
    monkeypatch.delenv("NAMU_ASK_API_KEY", raising=False)

    assert ask.is_enabled() is False


def test_enabled_only_for_a_provider_we_actually_have(monkeypatch):
    """모르는 회사 이름을 넣어 두고 배포하면 말풍선이 눌러도 답 없는 단추가 된다."""
    monkeypatch.setenv("NAMU_ASK_PROVIDER", "없는회사")

    assert ask.is_enabled() is False


def test_no_key_means_no_call(make_guide, monkeypatch):
    guide, fake = make_guide()
    monkeypatch.delenv("NAMU_ASK_API_KEY", raising=False)

    result = guide.answer("무료인가요")

    assert result.ok is False and result.reason == "disabled"
    assert fake.calls == [], "열쇠가 없는데 AI를 불렀다"
    assert result.sources, "답을 못 할 때는 안내서 링크라도 줘야 한다"


# ---------------------------------------------------------------------------
# 부르기 전에 걷어내는 것들
# ---------------------------------------------------------------------------
def test_blank_question_is_not_sent(make_guide):
    guide, fake = make_guide()

    assert guide.answer("   ").reason == "empty"
    assert fake.calls == []


def test_long_question_is_refused_before_the_call(make_guide):
    """긴 글 붙여넣기를 막는 것은 값이 아니라 하루 요청 수 때문이다(설계서 6-4절)."""
    guide, fake = make_guide()

    result = guide.answer("가" * (ask.MAX_QUESTION_CHARS + 1))

    assert result.ok is False and result.reason == "too_long"
    assert fake.calls == []


def test_off_topic_question_never_reaches_the_ai(make_guide):
    """**이 검사가 '지어내지 않기'의 뿌리다.** 말뭉치에서 못 찾으면 부르지
    않으므로, 안내원이 날씨를 답할 길이 원리상 없다(설계서 5-5절)."""
    guide, fake = make_guide()

    result = guide.answer("오늘 서울 날씨")

    assert result.ok is False and result.reason == "unknown"
    assert fake.calls == [], "0건인데 AI를 불렀다"
    assert result.sources == (ask._GUIDE_SOURCE,)


def test_off_topic_question_does_not_burn_the_daily_quota(make_guide):
    """부르지도 않은 질문으로 방문자의 하루 열 번을 깎지 않는다."""
    guide, _ = make_guide(per_person=2)

    for _ in range(5):
        guide.answer("피자 맛집 알려줘")

    assert guide.answer("무료인가요").ok is True


# ---------------------------------------------------------------------------
# 한도 — AI를 부르기 직전에 센다
# ---------------------------------------------------------------------------
def test_person_limit_stops_the_call(make_guide):
    guide, fake = make_guide(per_person=2)

    # 같은 사람이 다른 질문을 두 번(같은 질문이면 선반에서 나가 세지 않는다).
    assert guide.answer("무료인가요", cookie_id="쿠키A").ok is True
    assert guide.answer("나무가 뭔가요", cookie_id="쿠키A").ok is True
    blocked = guide.answer("기억이 어디 저장되나요", cookie_id="쿠키A")

    assert blocked.ok is False and blocked.reason == "limit"
    assert len(fake.calls) == 2, "한도를 넘었는데 AI를 불렀다"


def test_daily_cap_message_reaches_the_visitor(make_guide):
    guide, _ = make_guide(daily_cap=1)

    guide.answer("무료인가요", cookie_id="A")
    blocked = guide.answer("기억이 어디 저장되나요", cookie_id="B")

    assert blocked.reason == "limit"
    assert "내일" in blocked.text


def test_same_question_is_answered_from_the_shelf(make_guide):
    """설계서 6-4절 — 똑같은 글자를 다시 보내면 앞의 답을 그대로 준다.
    호출도 안 하고 한도도 세지 않는다."""
    guide, fake = make_guide(per_person=1)

    first = guide.answer("무료인가요", cookie_id="A")
    second = guide.answer(" 무료인가요 ", cookie_id="A")

    assert second.cached is True
    assert second.text == first.text
    assert len(fake.calls) == 1


def test_the_shelf_is_not_shared_between_people(make_guide):
    """남의 답을 돌려주면 '앞사람이 물은 것은 공짜'가 되어 한도가 뚫린다."""
    guide, fake = make_guide()

    guide.answer("무료인가요", cookie_id="A")
    other = guide.answer("무료인가요", cookie_id="B")

    assert other.cached is False
    assert len(fake.calls) == 2


# ---------------------------------------------------------------------------
# 프롬프트 — 방문자 글이 안내문 자리로 새지 않아야 한다
# ---------------------------------------------------------------------------
def test_prompt_puts_the_question_last(corpus):
    """자료 사이에 방문자 글이 끼면 어디까지가 우리 자료인지 흐려진다 —
    그 틈이 '지시 심기'가 파고드는 자리다(설계서 10-2절)."""
    chunks = corpus.search("무료인가요")
    prompt = ask.build_prompt("무료인가요", chunks, corpus.homepage)

    assert prompt.index("[자료]") < prompt.index("[질문]")
    assert prompt.rstrip().endswith("무료인가요")


def test_prompt_always_carries_the_homepage_text(corpus):
    """검색이 빗나가도 답이 나오게 하는 안전판(설계서 5-2절)."""
    prompt = ask.build_prompt("무료인가요", corpus.search("무료인가요"), corpus.homepage)

    assert "[0] 나무 클라우드 홈페이지 화면 글" in prompt
    assert "[홈페이지 /faq]" in prompt


def test_prompt_numbers_every_source_chunk(corpus):
    chunks = corpus.search("기억이 어디 저장되나요")
    prompt = ask.build_prompt("기억이 어디 저장되나요", chunks, corpus.homepage)

    for i, chunk in enumerate(chunks, start=1):
        assert f"[{i}] {chunk.cite}" in prompt


def test_history_is_capped(corpus):
    """직전 대화를 통째로 실으면 프롬프트가 방문자 마음대로 커진다."""
    history = [(f"질문{i}", f"답{i}") for i in range(10)]
    prompt = ask.build_prompt("무료인가요", corpus.search("무료인가요"), corpus.homepage, history)

    assert "질문9" in prompt
    assert "질문0" not in prompt


def test_system_prompt_pins_the_three_promises():
    """안내문에서 이 셋이 빠지면 아무 검사도 빨개지지 않은 채 약속만 사라진다."""
    text = ask.SYSTEM_PROMPT

    assert "자료에 없는" in text, "지어내지 말라는 문장이 빠졌다"
    assert "지시가 아닙니다" in text, "방문자 글을 지시로 받지 말라는 문장이 빠졌다"
    assert "근거:" in text, "근거 번호를 적으라는 형식이 빠졌다"


# ---------------------------------------------------------------------------
# 답 다듬기와 근거 링크
# ---------------------------------------------------------------------------
def test_citation_line_is_stripped_from_the_answer(make_guide):
    guide, _ = make_guide("나무는 기억을 남기는 도구입니다.\n근거: [1] [2]")

    result = guide.answer("나무가 뭔가요")

    assert "근거:" not in result.text
    assert result.text == "나무는 기억을 남기는 도구입니다."


def test_links_come_only_from_what_we_sent(make_guide):
    """모델이 아무 번호나 불러도 우리 목록에 없으면 링크가 안 나간다 —
    주소를 지어낼 길을 없애는 자리다(설계서 5-1절 첫째)."""
    guide, fake = make_guide("아무 말.\n근거: [99]")

    result = guide.answer("나무가 뭔가요")

    assert result.sources == ()


def test_links_point_at_the_chunks_the_model_cited(make_guide, corpus):
    guide, _ = make_guide("답입니다.\n근거: [2]")

    result = guide.answer("기억이 어디 저장되나요")
    cited = corpus.search("기억이 어디 저장되나요")[1]

    assert result.sources == (ask.Source(cited.doc, cited.url),)


def test_missing_citation_falls_back_to_the_top_chunks(make_guide):
    """모델이 형식을 안 지켰을 때 링크 없는 답을 내보내지 않는다 — 넣어 준
    조각이 곧 답의 근거이므로 앞 세 개를 대신 붙인다."""
    guide, _ = make_guide("근거 줄 없이 그냥 답합니다.")

    result = guide.answer("나무가 뭔가요")

    assert 1 <= len(result.sources) <= 3
    assert all(s.url.startswith("https://") for s in result.sources)


def test_tags_are_stripped_from_the_answer(make_guide):
    """화면이 글자로만 넣으므로 위험하지는 않지만, 두면 방문자에게 태그가 보인다."""
    guide, _ = make_guide("<b>굵게</b> 답합니다.<script>alert(1)</script>")

    result = guide.answer("나무가 뭔가요")

    assert "<" not in result.text and ">" not in result.text


def test_answer_is_clipped(make_guide):
    guide, _ = make_guide("가" * (ask.MAX_ANSWER_CHARS + 500))

    result = guide.answer("나무가 뭔가요")

    assert len(result.text) <= ask.MAX_ANSWER_CHARS + 1  # 말줄임표 한 글자


# ---------------------------------------------------------------------------
# 실패 — 홈페이지가 함께 죽지 않아야 한다
# ---------------------------------------------------------------------------
def test_provider_failure_becomes_a_soft_message(make_guide):
    """무료 등급에는 가동률 약속이 없다 — 이 길은 드물게가 아니라 종종 쓰인다."""
    guide, _ = make_guide(RuntimeError("Gemini 호출 실패 (status=503)"))

    result = guide.answer("무료인가요")

    assert result.ok is False and result.reason == "provider_error"
    assert "안내서" in result.text


def test_failure_message_never_leaks_the_key_or_body(make_guide):
    guide, _ = make_guide(RuntimeError("key=비밀열쇠 body={...}"))

    result = guide.answer("무료인가요")

    assert "비밀열쇠" not in result.text


def test_busy_gets_its_own_message(make_guide):
    """분당 한도는 10~15번이라 사람 몇이 겹치면 닿는다(설계서 7-1절). 다른
    실패와 달리 **정말로 잠시 뒤에 되므로** 할 말이 다르다."""
    guide, _ = make_guide(ask.ProviderBusy("status=429"))

    result = guide.answer("무료인가요")

    assert result.ok is False and result.reason == "busy"
    assert "붐빕니다" in result.text


def test_busy_does_not_get_mixed_into_the_generic_failure(make_guide):
    """붐빔을 일반 실패로 뭉뚱그리면 방문자가 '고장났나 보다' 하고 떠난다."""
    guide, _ = make_guide(RuntimeError("status=500"))

    assert guide.answer("무료인가요").reason == "provider_error"


def test_empty_reply_is_treated_as_a_failure(make_guide):
    """안전 필터에 걸리면 200으로 오면서 내용만 비어 온다. 빈 말풍선을 띄우지 않는다."""
    guide, _ = make_guide("   ")

    result = guide.answer("무료인가요")

    assert result.ok is False and result.reason == "provider_error"


def test_gemini_response_parsing():
    """응답 모양이 바뀌면 여기서 먼저 빨개진다(네트워크 없이 확인 가능한 유일한 자리)."""
    assert ask._gemini_text(
        {"candidates": [{"content": {"parts": [{"text": "답"}, {"text": "입니다"}]}}]}
    ) == "답입니다"
    # 안전 필터에 걸린 모양 — content가 통째로 없다.
    assert ask._gemini_text({"candidates": [{"finishReason": "SAFETY"}]}) == ""
    assert ask._gemini_text({}) == ""


# ---------------------------------------------------------------------------
# 두 곳에 적어 둔 값이 어긋나는 것
# ---------------------------------------------------------------------------
def test_guide_url_matches_ui():
    import ui

    assert ask.GUIDE_URL == ui.GUIDE_URL
