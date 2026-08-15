"""AI 안내원이 읽을 글(말뭉치) — 모으기·조각내기·낱말 검색.

설계서: `docs/namu_ai_guide_design.md` 4·5절.

이 파일이 하는 일은 넷이다.

1. **모은다** — 회원용 안내 문서를 읽어 태그를 걷고 글자만 남긴다(`_SOURCES`).
2. **조각낸다** — 제목을 기준으로 잘라 조각마다 "어느 문서 어느 대목"인지를
   붙인다(`chunk_text`). 이것이 답에 **근거 링크**를 정확히 달 수 있는 근거다.
3. **들고 있는다** — 조각 229개(74,330자)를 서버가 뜰 때 만들어 메모리에 둔다
   (`Corpus`). 만드는 데 16밀리초, 74KB다.
4. **찾는다** — 조각을 전부 훑어 점수를 매기고 높은 순으로 여섯 개(`Corpus.search`).

**홈페이지 화면 글은 검색 대상이 아니다.** 검색과 무관하게 늘 프롬프트에 들어가므로
(`homepage_text()`), 찾는 대상에 또 넣으면 같은 글이 두 번 들어간다.

── SQLite 색인을 쓰지 않는 이유 (2026-08-08 실측으로 방향을 바꿨다) ──

설계서 5-2절은 처음에 코어(`namu-plugin/db.py`)의 FTS5 세글자 색인을 그대로
쓰기로 했다. 만들어 재 보니 시험 질문 일곱 개 중 **넷이 0건**이었다 —
'무료인가요' · '어떻게 시작하나요' · '기억이 어디 저장되나요' · '터미널에서도
되나요'. 자세한 원인과 바꾼 근거는 아래 "4. 찾기" 앞의 설명에 적었다.

코어의 방식이 틀린 것이 아니다. 코어는 **회원이 쓴 기억**을 찾고, 우리는
**안내 문서**를 자연스러운 질문으로 찾는다 — 뒤쪽이 조사·어미를 훨씬 많이 달고
들어온다. 같은 도구가 두 자리에서 다르게 맞는 것뿐이다.
"""
import html as html_mod
import math
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

# 펴낸 안내서 사이트. ui.py의 같은 상수와 짝을 이룬다 — 여기서 다시 적는 이유는
# ask_corpus가 ui를 불러오면 (ui → pages → ask_corpus) 순환이 생기기 때문이다.
# 값이 어긋나면 test_ask_corpus.py가 잡는다.
_GUIDE_SITE = "https://onmiso-hash.github.io/namu-agent/docs"


@dataclass(frozen=True)
class Source:
    """말뭉치에 담을 문서 한 장."""

    path: str  # _REPO_ROOT 기준 상대 경로
    label: str  # 답에 적을 문서 이름
    url: str  # 방문자가 눌러서 열어볼 곳
    drop_sections: tuple = ()  # 이 제목으로 시작하는 대목은 통째로 뺀다


# ---------------------------------------------------------------------------
# 담을 문서 — 설계서 4-1절
#
# 개발 문서는 담지 않는다(설계서 4-2절, 사용자 결정). 그 판정은 파일 이름이
# 아니라 **안의 제목**으로 한다 — `docs/namu_attach_files.md`는 이름만 보면
# 안내서 같지만 실제 제목이 "겪은 사고 둘"·"코드 위치"인 완료 보고서라서 뺐다
# (2026-08-08 확인). 첨부 기능의 회원용 설명은 홈페이지 `/memory` 화면에 있고,
# 그 화면 글은 `homepage_text()`로 늘 들어간다.
# ---------------------------------------------------------------------------
_SOURCES = (
    Source(
        "vendor/namu-agent/docs/index.html",
        "나무 안내서 — 첫 화면",
        f"{_GUIDE_SITE}/index.html",
    ),
    Source(
        "vendor/namu-agent/docs/memory_architecture.html",
        "나무 안내서 — 기억 구조",
        f"{_GUIDE_SITE}/memory_architecture.html",
    ),
    Source(
        "vendor/namu-agent/docs/workflow_architecture.html",
        "나무 안내서 — 작업 흐름",
        f"{_GUIDE_SITE}/workflow_architecture.html",
    ),
    Source(
        "vendor/namu-agent/docs/remote_mcp_guide.html",
        "나무 안내서 — 직접 서버 운영하기",
        f"{_GUIDE_SITE}/remote_mcp_guide.html",
    ),
    Source(
        "vendor/namu-agent/docs/install_guide.html",
        "나무 안내서 — 플러그인 설치",
        f"{_GUIDE_SITE}/install_guide.html",
    ),
    Source(
        "vendor/namu-agent/README.ko.md",
        "나무 저장소 소개",
        "https://github.com/onmiso-hash/namu-agent/blob/main/README.ko.md",
    ),
    Source(
        "docs/namu_cloud_guide.md",
        "클라우드 사용 가이드",
        # 이 문서는 펴낸 사이트가 없다. 같은 절차를 담은 화면으로 보낸다 —
        # 없는 주소를 지어내 404를 주는 것보다 낫다.
        "https://namu-cloud.onnamu.kr/start",
        drop_sections=(
            # 폐기된 설계를 그대로 담은 대목. 안내원이 "지금 그렇다"고 말하면
            # 방문자가 없는 기능을 찾아 헤맨다(설계서 4-2절의 위험 그대로).
            "예전 모델(폐기됨)과의 차이",
        ),
    ),
)

# 바꿔 부르는 말 — 설계서 5-2절. 방문자가 쓰는 말과 문서가 쓰는 말이 다를 때
# 검색어에 함께 넣는다. 낱말 검색의 알려진 약점을 메우는 유일한 장치다.
_SYNONYMS = {
    "레포": ("저장소",),
    "리포": ("저장소",),
    "repo": ("저장소",),
    "repository": ("저장소",),
    "커넥터": ("접속", "주소"),
    "connector": ("접속", "주소"),
    "메모리": ("기억",),
    "memory": ("기억",),
    "토큰": ("열쇠", "접속"),
    "api": ("접속", "주소"),
    "mcp": ("접속", "주소"),
    "설치": ("플러그인",),
    "가입": ("시작", "로그인"),
    "요금": ("무료",),
    "가격": ("무료",),
    "비용": ("무료",),
    # '돈'을 여기 넣지 않는 이유 (2026-08-15에 넣었다가 되돌렸다) — 넣으면
    # '돈이 드나요'가 0건에서 5건이 되는데, **그 5건이 전부 딴 이야기다.**
    # 안내원이 읽는 글에서 '무료'가 나오는 대목은 GitHub 비공개 저장소가 공짜라는
    # 말과 바깥에서 들어올 통로가 공짜라는 말뿐이고, 이 서비스의 값에 대한 대목은
    # 하나도 없다(실측). 값을 묻는 방문자에게 저장소 요금 이야기를 꺼내는 것보다
    # 0건이 낫다 — 0건이면 안내원은 모른다고 답한다(search의 설명 참고).
    #
    # 값에 대한 답은 화면 '자주 묻는 질문'에 있는데 그 화면은 _SOURCES에 없다.
    # 안내원이 값을 답하게 하려면 낱말을 잇는 게 아니라 그 화면을 글감에
    # 넣어야 한다.
}

_CHUNK_CHARS = 800
_OVERLAP_CHARS = 150


# ---------------------------------------------------------------------------
# 1. 모으기 — 태그를 걷고 글자만 남긴다
# ---------------------------------------------------------------------------
_RE_DROP_BLOCK = re.compile(r"(?is)<(script|style|nav|header|footer)\b.*?</\1\s*>")
_RE_HEADING = re.compile(r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1\s*>")
_RE_LI = re.compile(r"(?is)<li\b[^>]*>")
_RE_BLOCK_END = re.compile(r"(?is)</(p|div|li|tr|section|article|ul|ol|pre|table)\s*>")
_RE_BR = re.compile(r"(?is)<br\s*/?>")
_RE_TAG = re.compile(r"(?s)<[^>]+>")


def html_to_text(raw: str) -> str:
    """HTML을 제목 표시(`#`)가 남은 글자로 바꾼다.

    제목을 살려 두는 이유가 이 함수의 존재 이유다 — 조각내기가 제목을 기준으로
    자르고, 조각마다 "어느 대목"인지를 붙여야 근거 링크에 대목 이름을 적을 수
    있다. 태그를 몽땅 지우면 8만 자짜리 글 한 덩어리가 되어 대목을 알 수 없다.

    상단 메뉴(`nav`·`header`)와 발(`footer`)은 통째로 버린다 — 문서마다 같은
    글이 반복돼 검색 결과를 오염시킨다.
    """
    text = _RE_DROP_BLOCK.sub("\n", raw)
    text = _RE_HEADING.sub(
        lambda m: "\n\n" + "#" * int(m.group(1)) + " " + _RE_TAG.sub("", m.group(2)).strip() + "\n",
        text,
    )
    text = _RE_LI.sub("\n- ", text)
    text = _RE_BR.sub("\n", text)
    text = _RE_BLOCK_END.sub("\n", text)
    text = _RE_TAG.sub(" ", text)
    text = html_mod.unescape(text)
    return _tidy(text)


def _tidy(text: str) -> str:
    """줄마다 공백을 접고, 빈 줄이 셋 이상 이어지면 둘로 줄인다."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def read_source(src: Source, root: "Path | None" = None) -> str:
    """문서 한 장을 글자로 읽는다. 마크다운은 그대로, HTML은 태그를 걷어서."""
    path = (root or _REPO_ROOT) / src.path
    raw = path.read_text(encoding="utf-8")
    return html_to_text(raw) if path.suffix.lower() in (".html", ".htm") else _tidy(raw)


def homepage_text() -> str:
    """홈페이지 화면 5장의 글 — 검색과 무관하게 늘 프롬프트에 들어간다.

    설계서 5-2절: 처음 온 사람 질문의 대부분이 이 다섯 장에서 끝나므로, 검색이
    빗나가도 답이 나오게 하는 안전판이다.

    `pages`를 여기서 불러오는 이유(파일 맨 위가 아니라 함수 안): `pages`가
    `ui`를 부르고 `ui`가 다시 이 파일을 부르므로, 맨 위에서 불러오면 순환이 된다.
    """
    import pages  # 지연 import — 위 설명 참고

    parts = []
    for path, render in pages.PAGES.items():
        parts.append(f"\n\n## [홈페이지 {path}]\n" + html_to_text(render(logged_in=False)))
    return _tidy("".join(parts))


# ---------------------------------------------------------------------------
# 2. 조각내기
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Chunk:
    """말뭉치 조각 하나 — 답의 근거로 그대로 인용되는 단위."""

    doc: str  # 문서 이름
    heading: str  # 제목 경로 (예: "2. 연결법 > 2-3. claude.ai에 붙이기")
    url: str
    text: str

    @property
    def cite(self) -> str:
        """프롬프트와 답에 함께 나갈 출처 한 줄."""
        where = f"{self.doc} — {self.heading}" if self.heading else self.doc
        return f"{where} ({self.url})"


_RE_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _sections(text: str) -> "list[tuple[list[str], str]]":
    """글을 (제목 경로, 본문) 목록으로 가른다. 제목이 없으면 한 덩어리."""
    stack: "list[str]" = []
    body: "list[str]" = []
    out: "list[tuple[list[str], str]]" = []

    def flush() -> None:
        joined = _tidy("\n".join(body))
        if joined:
            out.append((list(stack), joined))
        body.clear()

    for line in text.splitlines():
        m = _RE_MD_HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            del stack[level - 1 :]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(m.group(2))
        else:
            body.append(line)
    flush()
    return out


def _split_long(body: str) -> "list[str]":
    """긴 본문을 문단 경계에서 자른다(겹침 포함). 문장 중간에서 자르지 않는다."""
    if len(body) <= _CHUNK_CHARS:
        return [body]
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    pieces: "list[str]" = []
    cur = ""
    for para in paras:
        if cur and len(cur) + len(para) + 2 > _CHUNK_CHARS:
            pieces.append(cur)
            cur = cur[-_OVERLAP_CHARS:] + "\n\n" + para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        pieces.append(cur)
    # 문단이 하나뿐인데 상한을 넘는 경우(표·긴 목록) — 글자 수로 자른다.
    out: "list[str]" = []
    for piece in pieces:
        while len(piece) > _CHUNK_CHARS * 2:
            out.append(piece[:_CHUNK_CHARS])
            piece = piece[_CHUNK_CHARS - _OVERLAP_CHARS :]
        out.append(piece)
    return [p.strip() for p in out if p.strip()]


def chunk_text(text: str, doc: str, url: str, drop_sections: tuple = ()) -> "list[Chunk]":
    """글 한 장을 조각 목록으로 만든다."""
    chunks: "list[Chunk]" = []
    for stack, body in _sections(text):
        titles = [t for t in stack if t]
        if any(t in drop_sections for t in titles):
            continue
        heading = " > ".join(titles)
        for piece in _split_long(body):
            chunks.append(Chunk(doc=doc, heading=heading, url=url, text=piece))
    return chunks


def build_chunks(root: "Path | None" = None) -> "list[Chunk]":
    """담기로 한 문서 전부를 조각으로 만든다."""
    out: "list[Chunk]" = []
    for src in _SOURCES:
        out.extend(
            chunk_text(read_source(src, root), src.label, src.url, src.drop_sections)
        )
    return out


def data_dir() -> Path:
    """안내원 운영 데이터(색인·세기)가 놓일 폴더.

    `NAMU_ASK_DATA_DIR`로 정하고, 없으면 `NAMU_STORE_ROOT/ask`를 쓴다.
    **사용자 기억이 놓이는 `users/` 아래에는 절대 두지 않는다** — 운영 데이터와
    회원 기억을 같은 폴더에 섞지 않는 것이 이 저장소의 규칙이다
    (`src/identity.py` 파일 첫머리 설명 참고).
    """
    explicit = os.environ.get("NAMU_ASK_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit)
    store = os.environ.get("NAMU_STORE_ROOT", "").strip()
    if not store:
        raise RuntimeError(
            "NAMU_ASK_DATA_DIR 또는 NAMU_STORE_ROOT 중 하나는 설정돼 있어야 합니다 "
            "— AI 안내원의 색인과 세기 파일이 놓일 자리입니다."
        )
    return Path(store) / "ask"


# ---------------------------------------------------------------------------
# 4. 찾기 — 조각 229개를 그 자리에서 훑어 점수를 매긴다
#
# **왜 SQLite 세글자 색인을 쓰지 않나 (2026-08-08 실측으로 방향을 바꿨다)**
#
# 설계서 5-2절은 코어의 FTS5 세글자 색인을 그대로 쓰기로 했다. 만들어 재 보니
# 시험 질문 일곱 개 중 **넷이 0건**이었다.
#
#     '무료인가요' 0건 · '어떻게 시작하나요' 0건 ·
#     '기억이 어디 저장되나요' 0건 · '터미널에서도 되나요' 0건
#
# 원인이 둘이다.
#
# 1. **세글자 색인은 두 글자를 원리상 못 찾는다.** 그런데 방문자가 실제로 쓰는
#    말이 '무료'(문서에 6회)·'기억'(170회)·'가입'(5회)처럼 두 글자다. 코어는
#    이 경우 전수 조회로 우회하지만, 그래도 2번이 남는다.
# 2. **한국어는 낱말에 조사·어미가 붙는다.** '무료인가요'라는 글자열은 문서에
#    없다. 낱말을 통째로 맞추는 방식은 '무료인가요'를 '무료'로 이어 주지 못한다.
#
# 그래서 방식을 바꿨다. 말뭉치가 **229조각·74,330자**이고 만드는 데 **14밀리초**라,
# 색인 없이 그 자리에서 전부 훑어도 값이 들지 않는다. 대신 셋을 얻는다.
#
# - **꼬리를 떼고 찾는다** — '무료인가요'에서 앞자락을 줄여 가며 문서에 있는
#   가장 긴 형태('무료')를 쓴다. 조사·어미 목록을 관리하지 않아도 된다.
# - **흔한 말은 약하게 센다** — '기억'(170회)보다 '무료'(6회)가 그 질문을 더 잘
#   가리킨다. 문서에 드물게 나오는 말일수록 점수를 크게 준다.
# - **엉뚱한 질문에 0건을 돌려줄 수 있다** — 아래 `_MIN_COVERAGE` 참고.
# ---------------------------------------------------------------------------
_RE_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")

# 질문에서 뜻을 지지 않는 말. 빼는 이유는 점수가 아니라 **맞은 비율** 때문이다 —
# "오늘 서울 날씨"의 '오늘'처럼, 뜻 없는 말이 우연히 맞아 비율을 올리면 엉뚱한
# 질문에도 조각이 나간다.
_STOPWORDS = frozenset(
    """
    그 이 저 것 수 때 등 및 좀 잘 더 안 못 왜 뭐 뭔 뭔가요 무엇 무엇인가요
    어떻게 어디 어디에 언제 누가 누구 어느 얼마 얼마나 어떤 인가요 인가 나요
    까요 합니까 입니다 있나요 하나요 해요 되나요 될까요 하는 있는 되는 그리고
    하지만 그런데 만약 관련 대해 대한 위해 통해 사용 이용 방법 경우 정도
    a an the is are do does can could how what where when who why which
    """.split()
)

_MIN_COVERAGE = 0.5
"""질문의 뜻 있는 낱말 중 **절반 이상**이 말뭉치에 있어야 답을 시도한다.

이 한 줄이 "모르는 것을 지어내지 않기"의 장치다(설계서 10-2절).
실측으로 고른 값이다 — "오늘 서울 날씨"는 '오늘'만 맞아 1/3=0.33이라 걸러지고,
"무료인가요"는 '무료' 하나가 전부라 1/1=1.0으로 통과한다. 점수(드문 말일수록
높음)만으로는 이 둘을 가를 수 없었다: '오늘'은 문서에 2회뿐이라 오히려 점수가
높게 나온다.
"""


@dataclass(frozen=True)
class Term:
    """질문에서 뽑은 낱말 하나와, 말뭉치에서 실제로 찾을 형태."""

    raw: str  # 방문자가 친 그대로 (예: 무료인가요)
    form: str  # 말뭉치에 실제로 있는 가장 긴 형태 (예: 무료). 없으면 빈 값
    weight: float  # 드문 말일수록 크다


_MAX_TRIM = 3
"""꼬리를 최대 몇 글자까지 떼어 볼 것인가.

한국어 조사·어미는 길어야 세 글자다('~인가요'는 '무료인가요'에서 세 글자를 떼면
'무료'가 된다). **더 떼면 안 된다** — 실측에서 `asdfghjkl`을 두 글자까지 줄이자
'as'가 문서 어딘가에 맞아 엉뚱한 조각 세 개가 나왔다. 뜻 없는 글자를 억지로
말뭉치에 붙이는 셈이라, 안내원이 근거처럼 인용하게 된다.
"""


def _forms(token: str) -> "list[str]":
    """찾아볼 형태를 긴 것부터 — 낱말 그대로, 그다음 꼬리를 한 글자씩 떼며 (최대 셋).

    조사·어미 목록을 따로 관리하지 않는 이유: 목록은 반드시 빠뜨리는 것이 생기고,
    빠뜨린 것은 조용히 0건이 되어 아무도 모른다. 꼬리 떼기는 '무료인가요'→'무료',
    '저장소가'→'저장소', '기억은'→'기억'을 목록 없이 다 잡는다.
    """
    forms = [token]
    shortest = max(2, len(token) - _MAX_TRIM)
    forms.extend(token[:k] for k in range(len(token) - 1, shortest - 1, -1))
    forms.extend(_SYNONYMS.get(token, ()))
    return forms


class Corpus:
    """말뭉치 한 벌 — 조각 목록과 홈페이지 화면 글을 함께 들고 있다.

    서버가 뜰 때 한 번 만들고 계속 쓴다. 문서는 이미지 안에 고정돼 있어(제출
    태그로 핀이 박힌 서브모듈) 도중에 바뀌지 않으므로, 낡았는지 볼 일이 없다.
    """

    def __init__(self, chunks: "list[Chunk]", homepage: str):
        self.chunks = chunks
        self.homepage = homepage
        # **제목을 본문과 함께 찾는다.** 안 넣었더니 "claude.ai에 어떻게 붙이나요"가
        # 「2-3. claude.ai에 붙이기」라는 대목을 못 찾았다(2026-08-08 실측) — 그
        # 낱말이 제목에만 있었기 때문이다. 제목은 그 대목이 무엇에 대한 것인지를
        # 가장 압축해 담은 줄이라, 오히려 본문보다 잘 맞는다.
        self._folded = [f"{c.heading}\n{c.text}".lower() for c in chunks]
        lengths = [len(t) for t in self._folded]
        self._avg_len = (sum(lengths) / len(lengths)) if lengths else 1.0

    @classmethod
    def load(cls, root: "Path | None" = None) -> "Corpus":
        return cls(build_chunks(root), homepage_text())

    def _doc_freq(self, form: str) -> int:
        return sum(1 for t in self._folded if form in t)

    def _terms(self, query: str) -> "list[Term]":
        """질문을 낱말로 쪼개고, 각 낱말이 말뭉치에 있는 형태와 무게를 정한다."""
        norm = unicodedata.normalize("NFKC", query or "").lower()
        raws: "list[str]" = []
        seen: "set[str]" = set()
        for tok in _RE_TOKEN.findall(norm):
            if len(tok) < 2 or tok in _STOPWORDS or tok in seen:
                continue
            seen.add(tok)
            raws.append(tok)

        total = max(len(self.chunks), 1)
        terms: "list[Term]" = []
        for raw in raws:
            best_form, best_df = "", 0
            for form in _forms(raw):
                if len(form) < 2:
                    continue
                df = self._doc_freq(form)
                if df:
                    best_form, best_df = form, df
                    break  # 긴 형태부터 보므로 처음 맞은 것이 가장 긴 형태다
            # 드물수록 큰 무게. 말뭉치 전체에 있는 말(예: '나무')은 0에 가까워진다.
            weight = math.log(1 + total / (1 + best_df)) if best_form else 0.0
            terms.append(Term(raw=raw, form=best_form, weight=weight))
        return terms

    def search(self, query: str, limit: int = 6) -> "list[Chunk]":
        """질문에 맞는 조각을 점수 높은 순으로. 못 찾으면 빈 목록.

        **빈 목록을 돌려주는 것이 정상 동작이다** — 안내원은 조각이 없으면
        모른다고 답한다(설계서 10-2절). 억지로 뭔가를 채워 보내면 그 조각을
        근거처럼 인용한 그럴듯한 헛소리가 나온다.
        """
        terms = self._terms(query)
        if not terms:
            return []

        matched = [t for t in terms if t.form]
        if len(matched) / len(terms) < _MIN_COVERAGE:
            return []

        scored: "list[tuple[float, int]]" = []
        for i, folded in enumerate(self._folded):
            score = self._score(folded, matched)
            if score > 0:
                scored.append((score, i))
        if not scored:
            return []

        # 점수 내림차순, 같으면 문서 순서대로(결과가 매번 같아야 검사가 가능하다).
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [self.chunks[i] for _, i in scored[:limit]]

    # 몇 번 나왔는지를 점수에 반영하되 금세 완만해지게 하는 상수, 그리고 긴 조각이
    # 자동으로 유리해지는 것을 덜어내는 정도. 검색에서 오래 쓰인 값(BM25)이다.
    _TF_SATURATION = 1.2
    _LEN_NORMALIZE = 0.5

    def _score(self, folded: str, matched: "list[Term]") -> float:
        """조각 하나의 점수.

        **몇 번 나왔는지를 세는 이유** — 세지 않고 "있다/없다"로만 매기면 점수가
        같은 조각이 무더기로 나오고, 순위가 결국 문서를 늘어놓은 순서로 갈린다.
        실측에서 그 탓에 결과가 안내서 첫 화면 한 곳으로 쏠렸다(2026-08-08).

        **길이로 나누는 이유** — 안 나누면 긴 조각이 낱말을 더 많이 품어 늘 이긴다.
        문서마다 조각 길이가 평균 215자에서 505자까지 벌어져 있어 그냥 두면
        긴 문서가 검색을 독차지한다.
        """
        norm = 1 - self._LEN_NORMALIZE + self._LEN_NORMALIZE * (len(folded) / self._avg_len)
        total = 0.0
        for term in matched:
            tf = folded.count(term.form)
            if not tf:
                continue
            total += term.weight * (tf * (self._TF_SATURATION + 1)) / (
                tf + self._TF_SATURATION * norm
            )
        return total
