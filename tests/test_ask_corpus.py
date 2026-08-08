"""ask_corpus.py 유닛 테스트 — AI 안내원이 읽을 글과 낱말 검색.

여기서 지키는 것은 "검색이 잘 되나"보다 **틀리면 방문자가 속는 약속들**이다.

- 조각마다 근거 링크가 붙어 있는가 (없으면 안내원이 출처를 지어낸다)
- 개발 문서가 섞여 들어오지 않았는가 (폐기된 결정을 지금 사실처럼 말하게 된다)
- **엉뚱한 질문에 0건을 돌려주는가** (뭔가를 억지로 채워 주면 그럴듯한 헛소리가 된다)

검색 품질 검사는 실측으로 고른 질문 목록을 쓴다. 이 목록이 줄어들면 검사가
느슨해진 것이므로, 지울 때는 왜 지우는지 함께 적을 것.
"""
import ask_corpus
import pytest


@pytest.fixture(scope="module")
def corpus():
    return ask_corpus.Corpus.load()


# ---------------------------------------------------------------------------
# 담은 글 — 무엇이 들어오고 무엇이 안 들어오나
# ---------------------------------------------------------------------------
def test_every_chunk_carries_a_source_link():
    """근거 링크가 이 설계의 신뢰 장치다 — 조각 하나라도 빠지면 그 조각을 인용한
    답만 출처 없이 나가고, 그건 눈에 잘 안 띈다."""
    for chunk in ask_corpus.build_chunks():
        assert chunk.doc, "문서 이름이 없는 조각"
        assert chunk.url.startswith("https://"), f"열어볼 주소가 없는 조각: {chunk.doc}"
        assert chunk.text.strip()


def test_development_reports_are_not_in_the_corpus():
    """사용자 결정(설계서 4-2절) — 개발 문서는 담지 않는다.

    `docs/namu_attach_files.md`는 이름만 보면 안내서 같지만 "겪은 사고 둘"·
    "코드 위치"가 제목인 완료 보고서다. 이름으로 고르면 다시 섞여 들어온다.
    """
    paths = [src.path for src in ask_corpus._SOURCES]
    assert "docs/namu_attach_files.md" not in paths
    for path in paths:
        assert "plan" not in path, f"개발 계획 문서가 섞였다: {path}"


def test_retired_sections_are_dropped():
    """폐기된 설계를 담은 대목은 조각으로 만들지 않는다 — 안내원이 "지금
    그렇다"고 말하면 방문자가 없는 기능을 찾아 헤맨다."""
    chunks = ask_corpus.build_chunks()
    assert chunks, "조각이 하나도 안 만들어졌다"
    for chunk in chunks:
        assert "예전 모델(폐기됨)과의 차이" not in chunk.heading


def test_guide_site_matches_ui():
    """펴낸 안내서 주소를 두 곳에 적어 둔 탓에 한쪽만 고쳐지는 것을 막는다.

    (ask_corpus가 ui를 부르면 ui → pages → ask_corpus로 돌아오므로 상수를
    다시 적을 수밖에 없었다. 그 대신 이 검사가 짝을 지킨다.)
    """
    import ui

    assert ask_corpus._GUIDE_SITE == ui.GUIDE_SITE


def test_homepage_text_covers_all_five_public_pages():
    """홈페이지 화면 글은 검색과 무관하게 늘 프롬프트에 들어가는 안전판이다.
    한 장이라도 빠지면 그 화면에만 있는 답을 안내원이 영영 못 한다."""
    import pages

    text = ask_corpus.homepage_text()
    for path in pages.PAGES:
        assert f"[홈페이지 {path}]" in text
    assert len(text) > 3000


# ---------------------------------------------------------------------------
# 조각내기
# ---------------------------------------------------------------------------
def test_chunks_split_on_headings_not_mid_sentence():
    text = "# 큰제목\n\n첫 문단입니다.\n\n## 작은제목\n\n둘째 문단입니다."
    chunks = ask_corpus.chunk_text(text, "문서", "https://example.test")

    assert [c.heading for c in chunks] == ["큰제목", "큰제목 > 작은제목"]
    assert chunks[0].text == "첫 문단입니다."


def test_long_sections_are_split_with_overlap():
    """겹침이 없으면 문단 경계에 걸친 설명이 두 조각 어디에서도 온전하지 않다."""
    paras = [f"문단{i} " + "가" * 300 for i in range(6)]
    chunks = ask_corpus.chunk_text("\n\n".join(paras), "문서", "https://example.test")

    assert len(chunks) > 1
    assert all(len(c.text) <= ask_corpus._CHUNK_CHARS * 2 for c in chunks)
    # 앞 조각의 꼬리가 뒤 조각 머리에 남아 있어야 한다.
    assert chunks[0].text[-40:] in chunks[1].text


def test_html_headings_survive_tag_stripping():
    """제목을 살려 두는 것이 html_to_text의 존재 이유다 — 태그를 몽땅 지우면
    문서 한 장이 덩어리 하나가 되어 '어느 대목'을 알 수 없다."""
    out = ask_corpus.html_to_text(
        "<html><body><h2>연결법</h2><p>주소를 붙입니다.</p></body></html>"
    )

    assert "## 연결법" in out
    assert "주소를 붙입니다." in out


def test_navigation_and_footer_are_dropped():
    """문서마다 반복되는 메뉴·발이 들어오면 검색 결과가 그걸로 뒤덮인다."""
    out = ask_corpus.html_to_text(
        "<header>공통메뉴</header><p>본문</p><footer>공통발</footer>"
    )

    assert "공통메뉴" not in out and "공통발" not in out
    assert "본문" in out


# ---------------------------------------------------------------------------
# 찾기 — 실측으로 고른 질문 목록
# ---------------------------------------------------------------------------
# 방문자가 실제로 칠 법한 말로 적었다(문서에 있는 낱말을 그대로 옮기지 않았다).
# 한국어는 조사·어미가 붙어서, 문서의 말을 그대로 쓰면 검사가 통과해도 실제
# 질문에서는 0건이 나온다 — 실제로 그렇게 통과시켰다가 실측에서 걸렸다.
ANSWERABLE = [
    "나무가 뭔가요",
    "무료인가요",
    "어떻게 시작하나요",
    "기억이 어디 저장되나요",
    "터미널에서도 되나요",
    "레포는 어떻게 만드나요",
    "접속 주소를 잃어버렸어요",
    "claude.ai에 어떻게 붙이나요",
    "내 기억은 안전한가요",
    "그만두려면 어떻게 하나요",
    "파일도 올릴 수 있나요",
    "다른 PC에서도 되나요",
]

OFF_TOPIC = [
    "오늘 서울 날씨",
    "피자 맛집 알려줘",
    "asdfghjkl",
    "ㅁㄴㅇㄹ",
    "비트코인 시세",
]


@pytest.mark.parametrize("question", ANSWERABLE)
def test_answerable_questions_find_something(corpus, question):
    assert corpus.search(question), f"0건: {question}"


@pytest.mark.parametrize("question", OFF_TOPIC)
def test_off_topic_questions_find_nothing(corpus, question):
    """**빈 목록이 정상 동작이다.** 억지로 채워 보내면 안내원이 그 조각을
    근거처럼 인용한 그럴듯한 헛소리를 한다(설계서 10-2절).

    다만 검색이 유일한 문지기는 아니다 — 문서에 우연히 있는 낱말('코드' 등)로
    물으면 조각이 나올 수 있고, 그건 안내문(시스템 프롬프트)이 받는다.
    """
    assert corpus.search(question) == [], f"엉뚱한 질문에 답이 나왔다: {question}"


def test_search_finds_words_that_only_appear_in_headings(corpus):
    """제목을 검색에서 빼 뒀더니 「2-3. claude.ai에 붙이기」를 못 찾았다
    (2026-08-08 실측). 제목은 그 대목이 무엇에 대한 것인지 가장 압축한 줄이다."""
    hits = corpus.search("claude.ai에 어떻게 붙이나요", limit=6)

    assert hits
    assert any("claude" in (h.heading + h.text).lower() for h in hits)


def test_results_are_not_all_from_one_document(corpus):
    """점수가 같은 조각이 무더기로 나오면 순위가 문서를 늘어놓은 순서로 갈려,
    결과가 안내서 첫 화면 한 곳에 쏠린다(2026-08-08 실측)."""
    seen = {hit.doc for q in ANSWERABLE for hit in corpus.search(q, limit=6)}

    assert len(seen) >= 4, f"결과가 {len(seen)}개 문서에만 몰렸다: {seen}"


def test_word_endings_do_not_block_the_match(corpus):
    """'무료인가요'라는 글자열은 문서에 없다. 꼬리를 떼야 '무료'에 닿는다."""
    assert corpus.search("무료인가요")
    assert corpus.search("저장소가 뭔가요")


def test_trimming_stops_before_it_invents_a_match(corpus):
    """꼬리를 두 글자까지 떼면 'asdfghjkl'이 'as'로 줄어 문서 어딘가에 맞는다
    (2026-08-08 실측). 뜻 없는 글자를 억지로 말뭉치에 붙이는 셈이다."""
    forms = ask_corpus._forms("asdfghjkl")

    assert min(len(f) for f in forms) >= len("asdfghjkl") - ask_corpus._MAX_TRIM


def test_search_is_stable(corpus):
    """같은 질문에 매번 같은 순서가 나와야 검사가 가능하고, 방문자가 다시 물었을
    때 다른 답이 나오지 않는다."""
    first = corpus.search("기억이 어디 저장되나요", limit=6)
    second = corpus.search("기억이 어디 저장되나요", limit=6)

    assert [c.cite for c in first] == [c.cite for c in second]


def test_empty_and_stopword_only_queries_return_nothing(corpus):
    assert corpus.search("") == []
    assert corpus.search("   ") == []
    assert corpus.search("그 이 저 것") == []
