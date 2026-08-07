"""나무 클라우드 공개 페이지 — 홈·시작하기·무엇을 기억하나·안전·자주 묻는 질문.

파일 이름이 `site.py`가 아닌 이유: `site`는 파이썬이 기동할 때 이미 불러 두는
표준 모듈 이름이라, 같은 이름을 두면 우리 파일이 **영원히 안 불린다**(먼저
올라온 표준 모듈이 그대로 쓰인다). 조용히 어긋나는 종류의 사고라 이름을 피했다.

로그인이 필요 없는 화면만 여기 있다. 인증 코드(web_auth.py)와 섞지 않는 이유는
`ui.py` 첫머리에 적어 둔 것과 같다 — 공개 화면이 로그인 모듈에 의존하면 구조가
뒤집힌다. 이 파일은 `ui`만 부르고, 세션이 있는지 없는지는 **인자로 받는다**
(`logged_in`). 그래야 여기서 쿠키를 읽는 코드가 생기지 않는다.

**문구의 기준일: 2026-08-08.** 옛 안내서(`namu-agent/docs/*.html`)를 옮겨 오지
않았다. 그 문서들에는 폐기된 설명이 남아 있다 — 전원 공용 열쇠 + `?user=`
방식(namu-59에서 사라짐), "그릇은 둘, 곧 셋"(지금 다섯). 여기 적힌 것은 전부
지금 코드가 실제로 하는 일이다. 코드가 바뀌면 **이 파일이 먼저 틀리므로**,
동작을 바꿀 때 함께 고칠 것.

2026-08-08 현행화 — 이 파일이 실제로 틀어져 있던 것 셋을 고쳤다. ①"그릇은 셋"
(지금 다섯: 작업일지·첨부 기록이 늘었다) ②"작업일지는 이 주소로 남길 수 없다"
(지금은 남길 수 있다 — `routing_server.namu_record`의 `bowl == "tasks"` 분기)
③"기억 찾기는 교훈 그릇만"(지금은 다섯 그릇 전부 — 같은 파일 `namu_search`가
`cfg.BOWL_NAMES`를 그대로 받는다). 도구도 셋이 아니라 **열**이다(기억 3 + 첨부
7, `routing_server._EXPOSED`).

여기서 약속하지 않는 것 — 요금, 조직(Organization) 계정 지원 여부, 앞으로의
일정. 확인하지 않은 것을 페이지에 적으면 그 순간 서비스의 약속이 된다.
"""
import ui

# 접속 주소의 생김새를 보여줄 때 쓰는 예시. 진짜 열쇠가 아니라는 것이 한눈에
# 보이도록 사람이 읽는 자리표시자를 넣는다.
_URL_SHAPE = "https://namu-cloud.onnamu.kr/mcp/&lt;내-열쇠&gt;?client=claude"

# 저장소를 만들러 갈 때 쓰는, 이름과 비공개가 미리 채워진 GitHub 주소.
# 사이트가 대신 만들지 않는 이유는 `/safety` 페이지에 적어 두었다.
NEW_REPO_URL = "https://github.com/new?name=namu-memory&visibility=private"


# ---------------------------------------------------------------------------
# 첫인상 칸의 그림 — 기억이 한 장씩 쌓이는 모습.
#
# 글자가 없는 추상 도형이라 SVG로 그린다(글자가 들어가는 그림은 HTML 상자로
# 만든다 — 낭독기가 읽고 좁은 화면에서 줄바꿈되도록). 색은 CSS 변수를 그대로
# 받아 쓰므로 어두운 화면에서도 따로 손볼 것이 없다.
# ---------------------------------------------------------------------------
# 색을 그림 안에 적지 않고 CSS(`ui._PUBLIC_CSS`의 `.hero-art` 규칙)에 맡긴다.
# SVG 태그에 색 변수를 직접 쓰는 방식은 브라우저마다 처리가 갈려, 실패하면
# 도형이 통째로 검게 칠해진다 — 첫 화면에서 그 사고가 나면 손쓸 방법이 없다.
_HERO_ART = """
<svg viewBox="0 0 400 330" aria-hidden="true">
  <ellipse class="ha-shadow" cx="200" cy="292" rx="132" ry="15"/>
  <g class="ha-sheet">
    <rect x="74" y="228" width="252" height="52" rx="13"/>
    <rect x="88" y="176" width="224" height="52" rx="13"/>
    <rect x="102" y="124" width="196" height="52" rx="13"/>
  </g>
  <g class="ha-line">
    <rect x="96" y="246" width="104" height="7" rx="3.5"/>
    <rect x="96" y="261" width="66" height="7" rx="3.5"/>
    <rect x="110" y="194" width="94" height="7" rx="3.5"/>
    <rect x="110" y="209" width="58" height="7" rx="3.5"/>
    <rect x="124" y="142" width="84" height="7" rx="3.5"/>
    <rect x="124" y="157" width="50" height="7" rx="3.5"/>
  </g>
  <g class="ha-mark">
    <circle cx="292" cy="254" r="9"/>
    <circle cx="278" cy="202" r="9"/>
    <circle cx="264" cy="150" r="9"/>
  </g>
  <path class="ha-trunk" d="M196 124 L196 96"/>
  <g class="ha-crown">
    <circle cx="196" cy="62" r="40"/>
    <circle class="soft" cx="152" cy="80" r="25"/>
    <circle class="soft" cx="240" cy="80" r="25"/>
  </g>
</svg>
"""


def _flow_diagram() -> str:
    """기억이 실제로 어디에 있는지 — 세 칸과 화살표.

    이 그림이 사이트에서 가장 중요한 설명이다. "AI가 기억한다"는 말을 들은
    사람이 가장 먼저 하는 걱정이 '내 이야기가 어디에 쌓이느냐'이기 때문이다.
    """
    return (
        '<div class="flow">'
        '<div class="flow-node"><div class="ico" aria-hidden="true">💬</div>'
        "<h4>내가 쓰는 AI</h4>"
        "<p>claude.ai 같은 웹 AI에 주소 한 줄을 붙여 둡니다</p></div>"
        '<div class="flow-arrow" aria-hidden="true">→</div>'
        '<div class="flow-node"><div class="ico" aria-hidden="true">🌳</div>'
        "<h4>나무 클라우드</h4>"
        "<p>기억을 꺼내 주고 받아 적는 심부름꾼입니다</p></div>"
        '<div class="flow-arrow" aria-hidden="true">→</div>'
        '<div class="flow-node is-origin">'
        '<div class="ico" aria-hidden="true">🔒</div>'
        "<h4>내 GitHub 저장소</h4>"
        "<p><b>기억의 원본은 여기</b>에 파일로 쌓입니다</p></div>"
        "</div>"
        '<p class="flow-cap">나무 클라우드가 가진 것은 저장소의 <b>사본</b>뿐입니다. '
        "회원님이 연결을 끊어도 기억은 회원님 저장소에 그대로 남습니다.</p>"
    )


def _fork() -> str:
    """이 사이트가 맞는 사람인지 맨 앞에서 가른다.

    터미널 사용자를 이 길로 흘려보내면 반쪽짜리 나무를 쓰게 된다 — 이 주소로
    넘어가는 것은 기억과 파일뿐이고, 세션 브리핑·작업 절차·마무리 점검은
    플러그인에만 있다. 게다가 기억이 쌓이는 자리도 둘로 갈린다.
    """
    return (
        '<div class="fork">'
        '<div class="opt here"><span class="tag">이 사이트가 맞습니다</span>'
        "<h4>브라우저에서 AI를 쓴다</h4>"
        "<p>claude.ai·ChatGPT처럼 웹에서 쓰는 AI에 주소 한 줄을 붙이면 "
        "그때부터 기억이 이어집니다.</p>"
        '<a class="btn btn-primary" href="/start">시작하는 법 보기</a></div>'
        '<div class="opt"><span class="tag">여기가 아닙니다</span>'
        "<h4>터미널에서 AI를 쓴다</h4>"
        "<p>Claude Code·agy를 쓰신다면 주소를 붙이는 것이 아니라 나무를 "
        "<b>플러그인으로 설치</b>하셔야 합니다. 이 주소로는 기억과 파일만 "
        "넘어가고 나머지 절반이 따라오지 않습니다.</p>"
        f'<a class="btn" href="{ui.INSTALL_GUIDE_URL}" target="_blank" '
        'rel="noopener">플러그인 설치 안내서 ↗</a></div>'
        "</div>"
    )


def _cta_row(logged_in: bool) -> str:
    if logged_in:
        return (
            '<div class="hero-cta">'
            '<a class="btn btn-primary btn-lg" href="/auth/me">내 페이지로</a>'
            '<a class="btn btn-lg" href="/start">시작하는 법 다시 보기</a>'
            "</div>"
        )
    return (
        '<div class="hero-cta">'
        '<a class="btn btn-primary btn-lg" href="/auth/github/login">'
        "GitHub으로 시작하기</a>"
        '<a class="btn btn-lg" href="/start">먼저 둘러보기</a>'
        "</div>"
        '<p class="btn-note">GitHub 계정만 있으면 됩니다 · 카드 등록 없음</p>'
    )


# ---------------------------------------------------------------------------
# 홈
# ---------------------------------------------------------------------------
def home_page(logged_in: bool = False) -> str:
    hero = (
        '<div class="hero"><div class="hero-in">'
        "<div>"
        '<span class="eyebrow">🌳 웹에서 쓰는 AI를 위한 기억</span>'
        "<h1>대화가 끝나도<br>남는 AI 기억</h1>"
        '<p class="lead">오늘 알려준 것을 내일 또 설명하지 않아도 됩니다. '
        "기억은 회원님의 GitHub 저장소에 파일로 쌓이고, 어떤 AI에서든 같은 "
        "기억을 꺼내 씁니다.</p>"
        + _cta_row(logged_in)
        + "</div>"
        f'<div class="hero-art" aria-hidden="true">{_HERO_ART}</div>'
        "</div></div>"
    )

    where = ui.section(
        _flow_diagram(),
        eyebrow="가장 먼저 알아야 할 것",
        title="내 기억은 내 자리에 쌓입니다",
        sub="나무는 회원님의 기억을 자기 것으로 갖지 않습니다. "
        "회원님이 고른 저장소 <b>한 칸</b>에만 손이 닿습니다.",
        band=True,
    )

    fork = ui.section(
        _fork(),
        eyebrow="시작하기 전에",
        title="어디서 AI를 쓰시나요?",
        sub="두 길은 설치 방법이 아예 다릅니다. 여기서 한 번만 갈라 두면 "
        "나중에 기억이 두 곳으로 흩어지는 일이 없습니다.",
    )

    how = ui.section(
        ui.steps(
            [
                (
                    "GitHub으로 로그인",
                    "<p>회원님이 누구인지만 확인합니다. 이 단계에서는 아직 "
                    "저장소를 들여다보지 않습니다.</p>",
                ),
                (
                    "기억을 담을 저장소 준비",
                    "<p>비공개 저장소 하나면 됩니다. 없으면 이름이 미리 채워진 "
                    "화면으로 보내 드립니다 — 만들기 단추 한 번이면 끝납니다.</p>",
                ),
                (
                    "그 저장소에만 권한 주기",
                    "<p>나무가 볼 수 있는 것은 회원님이 고른 저장소 하나뿐입니다. "
                    "나머지 저장소는 목록조차 넘어가지 않습니다.</p>",
                ),
                (
                    "받은 주소를 AI에 붙이기",
                    "<p>주소 한 줄을 복사해 AI의 커넥터에 붙이면 끝입니다. "
                    "그 뒤로는 대화 중에 기억을 꺼내고 남길 수 있습니다.</p>",
                ),
            ]
        )
        + '<p style="margin-top:22px"><a class="btn" href="/start">'
        "화면 그대로 따라가기 →</a></p>",
        eyebrow="네 걸음",
        title="처음 한 번만 거치면 됩니다",
        sub="5분이면 충분합니다. 중간에 창을 닫아도 <b>내 페이지</b>에서 "
        "이어서 하실 수 있습니다.",
        band=True,
    )

    gains = ui.section(
        '<div class="grid grid-3">'
        '<div class="card"><h3>🔒 비공개 저장소 하나</h3>'
        "<p class='muted'>회원님 GitHub 안에 생깁니다. 기억이 전부 여기 "
        "파일로 쌓이고, 회원님이 직접 열어 보실 수 있습니다.</p></div>"
        '<div class="card"><h3>🔑 나만의 접속 주소</h3>'
        "<p class='muted'>AI에 붙이는 주소 한 줄입니다. 언제든 새로 발급하거나 "
        "아예 없앨 수 있습니다.</p></div>"
        '<div class="card"><h3>📖 기억을 보는 화면</h3>'
        "<p class='muted'>AI가 무엇을 기억했는지 사람 눈으로 확인하고 찾아볼 "
        "수 있습니다.</p></div>"
        "</div>",
        eyebrow="무엇이 생기나",
        title="가입하면 이 세 가지가 남습니다",
    )

    trust = ui.section(
        '<div class="grid grid-2">'
        '<div class="claim"><span class="ic" aria-hidden="true">🔒</span>'
        "<p><b>원본은 회원님 것입니다</b>기억은 회원님 저장소에 쌓이고, 서버는 "
        "그 사본을 두고 읽고 씁니다.</p></div>"
        '<div class="claim"><span class="ic" aria-hidden="true">🎯</span>'
        "<p><b>저장소 하나만 봅니다</b>회원님이 고른 저장소 한 칸 밖으로는 "
        "손이 닿지 않습니다.</p></div>"
        '<div class="claim"><span class="ic" aria-hidden="true">⏱️</span>'
        "<p><b>열쇠를 쥐고 있지 않습니다</b>GitHub 접근은 그때그때 받는 "
        "짧은 수명(1시간)의 표를 씁니다.</p></div>"
        '<div class="claim"><span class="ic" aria-hidden="true">🚪</span>'
        "<p><b>언제든 끊을 수 있습니다</b>주소를 폐기하면 그 순간부터 어떤 "
        "AI도 회원님 기억에 닿지 못합니다.</p></div>"
        "</div>"
        '<p style="margin-top:20px"><a class="btn" href="/safety">'
        "안전에 대해 자세히 →</a></p>",
        eyebrow="안심하셔도 되는 이유",
        title="맡기는 것이 아니라, 빌려주는 것입니다",
        band=True,
    )

    closing = ui.section(
        '<div class="card card-accent" style="text-align:center">'
        "<h3 style='margin-top:6px'>지금 시작하시겠어요?</h3>"
        "<p class='muted'>GitHub 계정만 있으면 5분이면 끝납니다.</p>"
        + (
            '<a class="btn btn-primary btn-lg" href="/auth/me">내 페이지로</a>'
            if logged_in
            else '<a class="btn btn-primary btn-lg" href="/auth/github/login">'
            "GitHub으로 시작하기</a>"
        )
        + "</div>"
    )

    return ui.page(
        "나무 클라우드 — 대화가 끝나도 남는 AI 기억",
        hero + where + fork + how + gains + trust + closing,
        current="/",
        cta="me" if logged_in else "start",
        description="웹에서 쓰는 AI에 기억을 붙여 주는 서비스. 기억의 원본은 "
        "회원님의 GitHub 저장소에 파일로 쌓입니다.",
        reveal=True,
        raw_body=True,
    )


# ---------------------------------------------------------------------------
# 시작하기
# ---------------------------------------------------------------------------
def start_page(logged_in: bool = False) -> str:
    body = (
        '<span class="eyebrow">시작하기</span>'
        "<h1>가입부터 첫 기억까지</h1>"
        '<p class="lead">화면이 시키는 대로만 따라오시면 됩니다. '
        "여기 적힌 것은 실제로 뜨는 화면 그대로입니다.</p>"
        + ui.notice(
            "<b>터미널에서 AI를 쓰신다면 이 길이 아닙니다.</b> Claude Code·agy는 "
            f'주소가 아니라 <a href="{ui.INSTALL_GUIDE_URL}" target="_blank" '
            'rel="noopener">플러그인 설치</a>가 맞습니다.',
            tone="warn",
        )
        + "<h2>준비물</h2>"
        "<ul><li><b>GitHub 계정</b> — 없으면 먼저 만드셔야 합니다. 무료입니다.</li>"
        "<li><b>기억을 담을 저장소 하나</b> — 없어도 됩니다. 아래 2번에서 "
        "만듭니다.</li></ul>"
        + ui.steps(
            [
                (
                    "GitHub으로 로그인합니다",
                    '<p><a class="btn btn-primary" href="/auth/github/login">'
                    "GitHub으로 시작하기</a></p>"
                    "<p>GitHub이 <b>신원 확인</b> 화면을 보여줍니다. 이 화면에는 "
                    "저장소 권한 이야기가 나오지 않습니다 — 정상입니다. "
                    "권한은 3번에서 따로 묻습니다.</p>",
                ),
                (
                    "기억을 담을 저장소를 마련합니다",
                    "<p>저장소는 회원님 GitHub 안의 폴더 하나라고 보시면 됩니다. "
                    "나무가 남기는 기억이 전부 이 안에 파일로 쌓입니다.</p>"
                    f'<p><a class="btn" href="{NEW_REPO_URL}" target="_blank" '
                    'rel="noopener">GitHub에서 만들기 ↗</a></p>'
                    "<p>이름(<code>namu-memory</code>)과 <b>비공개</b>가 미리 채워진 "
                    "채로 열립니다 — 만들기 단추 한 번만 누르고 돌아오세요. "
                    "이미 쓰던 저장소가 있다면 그걸 쓰셔도 됩니다.</p>",
                ),
                (
                    "그 저장소에만 권한을 줍니다",
                    "<p>이번에는 저장소 접근을 묻는 <b>다른 화면</b>이 뜹니다. "
                    "여기서 방금 만든 저장소 하나만 고르세요.</p>"
                    "<p>여러 개를 고르셨다면 다음 화면에서 기억을 담을 저장소를 "
                    "하나 정하게 됩니다.</p>",
                ),
                (
                    "받은 주소를 AI에 붙입니다",
                    f"<p>연결이 끝나면 이런 모양의 주소가 화면에 뜹니다.</p>"
                    f"<pre><code>{_URL_SHAPE}</code></pre>"
                    "<p>클로드 기준으로는 <b>설정 → 커넥터 → 사용자 정의 커넥터 "
                    "추가</b>에 그대로 붙여 넣고 저장하시면 됩니다. 이름은 아무거나 "
                    "(예: 나무) 적으셔도 됩니다.</p>"
                    "<p>다른 AI에 붙일 때는 주소 끝의 <code>client=claude</code>를 "
                    "그 AI 이름으로 바꾸세요(예: <code>client=chatgpt</code>). "
                    "나중에 '어느 AI가 남긴 기억인지' 골라 찾을 때 쓰는 "
                    "이름표라, 한 번 정하면 계속 같은 값을 쓰셔야 합니다.</p>",
                ),
            ]
        )
        + "<h2>붙이고 나면</h2>"
        "<p>대화 중에 AI가 <b>기억 꺼내기</b>, <b>기억 남기기</b>, "
        "<b>기억 찾기</b>를 할 수 있게 됩니다. 여기에 <b>파일 올리고 받기</b>도 "
        "함께 켜집니다. 처음에는 \"지금까지 내 기억 꺼내 봐\"처럼 말로 시켜 "
        "보시면 됩니다.</p>"
        '<div class="card card-soft">'
        "<h4>주소를 잃어버렸다면</h4>"
        '<p class="muted" style="margin:0">창을 닫으셨어도 괜찮습니다. '
        '<a href="/auth/me">내 페이지</a>에 언제나 그대로 있습니다.</p>'
        "</div>"
        + ui.notice(
            "<b>이 주소는 사실상 비밀번호입니다.</b> 아는 사람은 회원님 기억을 "
            "읽고 쓸 수 있습니다 — 채팅·화면 캡처에 그대로 올리지 마세요. "
            '새어 나갔다면 <a href="/safety">주소를 새로 발급</a>하면 옛 주소는 '
            "즉시 막힙니다.",
            tone="bad",
        )
    )
    return ui.page(
        "시작하기 — 나무 클라우드",
        body,
        current="/start",
        cta="me" if logged_in else "start",
        description="나무 클라우드 가입부터 웹 AI에 접속 주소를 붙이기까지, "
        "네 걸음 안내.",
    )


# ---------------------------------------------------------------------------
# 무엇을 기억하나
# ---------------------------------------------------------------------------
def memory_page(logged_in: bool = False) -> str:
    body = (
        '<span class="eyebrow">무엇을 기억하나</span>'
        "<h1>기억은 세 층으로 남습니다</h1>"
        '<p class="lead">한 줄만 남기면 나중에 왜 그랬는지 알 수 없고, '
        "원문만 남기면 목록에서 읽을 수가 없습니다. 그래서 한 건마다 세 층을 "
        "함께 적습니다.</p>"
        '<div class="grid grid-3">'
        '<div class="card"><span class="pill">1층</span><h3>무엇을</h3>'
        "<p class='muted'>한 줄 요약입니다. 목록에 그대로 실리는 유일한 "
        "층이라, 남길 때 한 번 쓰고 고정합니다.</p></div>"
        '<div class="card"><span class="pill">2층</span><h3>왜</h3>'
        "<p class='muted'>어떻게 알게 됐고 왜 남기는지. 이게 없으면 나중에 "
        "같은 이야기를 처음부터 다시 하게 됩니다.</p></div>"
        '<div class="card"><span class="pill">3층</span><h3>그때 무슨 일이</h3>'
        "<p class='muted'>원문과 경위 전부. 길이 제한이 없어서 조사한 자료를 "
        "통째로 담아 둘 수 있습니다.</p></div>"
        "</div>"
        "<h2>담기는 그릇은 다섯 가지입니다</h2>"
        "<p>아무 데나 쌓지 않고 성격에 따라 나눠 담습니다. 섞어 두면 나중에 "
        "찾을 수가 없기 때문입니다.</p>"
        "<table><thead><tr><th>그릇</th><th>무엇을 담나</th><th>예</th></tr>"
        "</thead><tbody>"
        "<tr><td><b>교훈</b></td><td>다시 쓸 배움</td>"
        "<td>이 방식은 이래서 실패했다</td></tr>"
        "<tr><td><b>개인 사실</b></td><td>회원님에 대한 사실</td>"
        "<td>나는 표를 좋아하고 긴 글을 싫어한다</td></tr>"
        "<tr><td><b>작업일지</b></td><td>일의 진행 기록</td>"
        "<td>어디까지 했고 다음엔 무엇부터</td></tr>"
        "<tr><td><b>쪽지</b></td><td>쓰고 버릴 메모</td>"
        "<td>방금 조사한 자료 원문</td></tr>"
        "<tr><td><b>첨부 기록</b></td><td>올린 파일의 이력</td>"
        "<td>이 보고서를 왜 올렸나</td></tr>"
        "</tbody></table>"
        '<div class="card card-soft">'
        "<h4>쪽지만 지울 수 있습니다</h4>"
        "<p class='muted' style='margin:0'>교훈과 개인 사실은 화면에서 고치거나 "
        "지울 수 없습니다. 쌓인 배움을 나중에 손대면 <b>그때 무엇을 알았는지</b>가 "
        "남지 않기 때문입니다. 틀린 내용은 AI에게 말해 새 사실로 정정하시면 "
        "옛 항목이 물러납니다.</p>"
        "</div>"
        "<h2>파일도 함께 맡아 둡니다</h2>"
        "<p>대화에서 만든 글이나 손에 든 파일을 올려 두고, 나중에 어느 AI에서든 "
        "그 하나만 다시 받을 수 있습니다. 파일은 <b>회원님 저장소</b>로 바로 "
        "가고, 나무는 무슨 파일을 왜 올렸는지라는 <b>이력</b>만 갖습니다. "
        "그림·PDF처럼 글자가 아닌 파일이나 큰 파일은 <b>일회용 링크</b>를 "
        "만들어 주고받는데, 이때는 파일이 AI를 아예 안 거쳐서 크기와 상관없이 "
        "빠릅니다.</p>"
        "<h2>작업일지도 이 주소로 남길 수 있습니다</h2>"
        "<p>한 가지만 다릅니다 — <b>남길 때는 어느 프로젝트인지 적어야</b> "
        "합니다. PC에서 도는 나무는 지금 열어 둔 폴더가 곧 프로젝트지만, "
        "웹에는 그 개념이 없기 때문입니다. 그냥 <b>보기만</b> 할 때는 안 적어도 "
        "전체를 합쳐서 보여줍니다.</p>"
        "<h2>지금은 이렇게 동작합니다</h2>"
        "<ul>"
        "<li><b>기억 꺼내기</b>는 여러 그릇을 한꺼번에 훑어 최근 것을 "
        "돌려줍니다.</li>"
        "<li><b>기억 찾기</b>는 <b>다섯 그릇 전부</b>를 낱말로 찾습니다. "
        "띄어쓴 낱말이 <b>전부 들어 있는 것</b>만 걸리고, 순서는 상관없습니다.</li>"
        "<li>같은 기억을 어느 AI에서 붙이든 똑같이 꺼내 쓸 수 있습니다. "
        "누가 남겼는지는 주소 끝의 이름표로 구분됩니다.</li>"
        "</ul>"
        '<p style="margin-top:26px">'
        + (
            '<a class="btn btn-primary" href="/auth/memory">내 기억 열어 보기</a>'
            if logged_in
            else '<a class="btn btn-primary" href="/auth/github/login">'
            "GitHub으로 시작하기</a>"
        )
        + "</p>"
    )
    return ui.page(
        "무엇을 기억하나 — 나무 클라우드",
        body,
        current="/memory",
        cta="me" if logged_in else "start",
        description="나무가 남기는 기억의 세 층(무엇을·왜·그때 무슨 일이)과 "
        "다섯 그릇(교훈·개인 사실·작업일지·쪽지·첨부 기록), 그리고 파일 주고받기.",
    )


# ---------------------------------------------------------------------------
# 안전
# ---------------------------------------------------------------------------
def safety_page(logged_in: bool = False) -> str:
    body = (
        '<span class="eyebrow">안전</span>'
        "<h1>기억을 맡기기 전에</h1>"
        '<p class="lead">기억은 개인적인 자료입니다. 그래서 이 서비스가 '
        "무엇을 할 수 있고 무엇을 못 하는지 숨기지 않고 적습니다.</p>"
        "<h2>원본은 회원님 저장소에 있습니다</h2>"
        "<p>나무 클라우드는 회원님 저장소의 <b>사본</b>을 서버에 두고, 기억을 "
        "읽고 쓴 뒤 회원님 저장소로 다시 밀어 올립니다. 서비스를 그만두셔도 "
        "기억은 회원님 저장소에 파일로 그대로 남습니다.</p>"
        "<h2>저장소 하나만 봅니다</h2>"
        "<p>나무가 접근할 수 있는 것은 회원님이 권한을 줄 때 직접 고른 저장소 "
        "<b>한 칸</b>뿐입니다. 다른 저장소는 목록조차 넘어오지 않습니다. "
        "접근에는 그때그때 발급받는 <b>1시간짜리 표</b>를 쓰고, 영구적인 열쇠를 "
        "보관하지 않습니다.</p>"
        '<div class="card card-soft">'
        "<h4>왜 저장소를 대신 만들어 주지 않나요</h4>"
        "<p class='muted' style='margin:0'>GitHub은 '저장소 만들기' 권한만 따로 "
        "주지 않습니다 — <b>만들기·지우기·설정 바꾸기</b>가 한 덩어리입니다. "
        "그러면 모든 회원의 승인 화면에 <b>\"이 앱이 저장소를 삭제할 수 "
        "있습니다\"</b>라는 줄이 뜹니다. 기억을 맡기는 서비스가 클릭 한 번 "
        "줄이자고 치를 값이 아니라고 봤습니다. 대신 이름과 비공개가 미리 "
        "채워진 화면으로 보내 드립니다.</p>"
        "</div>"
        "<h2>접속 주소가 곧 신분증입니다</h2>"
        "<p>이 서비스의 인증은 주소에 실린 <b>개인 열쇠 하나</b>입니다. 서버는 "
        "요청마다 그 열쇠로 누구인지 판정하고, 모르는 열쇠는 그 자리에서 "
        "끊습니다. 바꿔 말하면 <b>주소를 아는 사람은 회원님 본인으로 "
        "인정됩니다.</b></p>"
        + ui.notice(
            "채팅·이슈·화면 캡처에 주소를 그대로 올리지 마세요. 실수로 "
            "새어 나갔다면 내 페이지에서 <b>재발급</b>하시면 옛 주소는 그 즉시 "
            "막힙니다.",
            tone="warn",
        )
        + "<h2>주소는 내 페이지에서 관리합니다</h2>"
        + ui.steps(
            [
                (
                    "연결 시험",
                    "<p>지금 이 주소가 실제로 응답하는지 서버가 대신 확인합니다. "
                    "판정은 <b>살아있음 · 주소가 잘못됨 · 지금은 확인 불가</b> "
                    "셋입니다.</p>"
                    "<p class='muted'>마지막 판정은 <b>주소가 잘못됐다는 뜻이 "
                    "아닙니다</b> — 서버를 새로 올린 직후 등 일시적인 상황입니다. "
                    "1~2분 뒤 다시 눌러 보세요.</p>",
                ),
                (
                    "주소 재발급",
                    "<p>새 주소를 만들고 옛 주소를 즉시 막습니다. AI에 등록해 둔 "
                    "커넥터 주소도 새것으로 바꿔 주셔야 합니다.</p>",
                ),
                (
                    "주소 폐기",
                    "<p>주소를 아예 없앱니다. 그 순간부터 어떤 AI도 회원님 기억에 "
                    "닿지 못합니다. 기억 자체는 저장소에 남으므로, 나중에 다시 "
                    "발급받으면 이어서 쓰실 수 있습니다.</p>",
                ),
            ]
        )
        + "<h2>완전히 그만두려면</h2>"
        "<ol>"
        "<li>내 페이지에서 <b>주소를 폐기</b>합니다.</li>"
        "<li>GitHub 설정에서 나무 앱의 <b>권한을 회수</b>합니다.</li>"
        "<li>기억이 담긴 저장소는 회원님 것이므로 그대로 두셔도 되고, "
        "직접 지우셔도 됩니다.</li>"
        "</ol>"
        '<p style="margin-top:26px">'
        + (
            '<a class="btn btn-primary" href="/auth/me">내 페이지 열기</a>'
            if logged_in
            else '<a class="btn btn-primary" href="/auth/github/login">'
            "GitHub으로 시작하기</a>"
        )
        + "</p>"
    )
    return ui.page(
        "안전 — 나무 클라우드",
        body,
        current="/safety",
        cta="me" if logged_in else "start",
        description="기억의 원본이 어디에 있는지, 나무가 무엇에 접근하는지, "
        "주소를 어떻게 관리하고 그만두는지.",
    )


# ---------------------------------------------------------------------------
# 자주 묻는 질문
# ---------------------------------------------------------------------------
def faq_page(logged_in: bool = False) -> str:
    body = (
        '<span class="eyebrow">자주 묻는 질문</span>'
        "<h1>궁금하실 만한 것들</h1>"
        '<p class="lead">여기 없는 것이 궁금하시면 GitHub 저장소에 남겨 '
        "주세요.</p>"
        + ui.faq(
            [
                (
                    "돈이 드나요?",
                    "<p>지금은 소수 사용자와 함께 써 보는 단계라 요금을 받지 "
                    "않습니다. 요금제는 아직 정해진 것이 없으며, 생기면 미리 "
                    "알려 드립니다. 카드 정보를 받지 않습니다.</p>",
                ),
                (
                    "저장소가 뭔가요?",
                    "<p>회원님 GitHub 안의 폴더 하나라고 보시면 됩니다. 나무가 "
                    "남기는 기억이 전부 그 안에 글자 파일로 쌓입니다. 회원님이 "
                    "직접 열어 읽으실 수 있고, 그대로 내려받을 수도 "
                    "있습니다.</p>",
                ),
                (
                    "저장소를 비공개로 해도 되나요?",
                    "<p>오히려 비공개를 권합니다. 만드는 화면으로 보내 드릴 때 "
                    "비공개가 미리 골라진 채로 열립니다.</p>",
                ),
                (
                    "다른 컴퓨터에서도 쓸 수 있나요?",
                    "<p>됩니다. 기억은 회원님 저장소에 있고 주소만 있으면 어디서 "
                    "쓰든 같은 기억을 꺼냅니다. 브라우저가 있는 기기라면 "
                    "됩니다.</p>",
                ),
                (
                    "여러 AI에 같이 붙여도 되나요?",
                    "<p>됩니다. 주소 끝의 이름표만 그 AI 이름으로 바꿔 붙이세요"
                    "(예: <code>client=chatgpt</code>). 기억은 한 곳에 모이고, "
                    "누가 남겼는지는 그 이름표로 구분됩니다.</p>"
                    "<p><b>한 번 정한 이름은 계속 같은 것을 쓰셔야 합니다</b> — "
                    "<code>claude</code>와 <code>cld</code>는 서로 다른 것으로 "
                    "저장됩니다.</p>",
                ),
                (
                    "AI가 내 기억을 다 읽나요?",
                    "<p>대화 중에 AI가 기억을 꺼내면 그 내용이 그 대화에 "
                    "들어갑니다. 그러니 남기고 싶지 않은 것은 애초에 남기지 않는 "
                    "편이 낫습니다. 무엇이 남았는지는 <b>내 기억</b> 화면에서 "
                    "언제든 확인하실 수 있습니다.</p>",
                ),
                (
                    "Claude Code나 agy를 쓰는데요?",
                    "<p>그 경우에는 이 주소를 붙이지 마세요. 주소로 넘어가는 "
                    "것은 기억과 파일뿐이고, 세션 브리핑·작업 절차·마무리 "
                    "점검처럼 나무의 나머지 절반이 따라오지 않습니다. 게다가 "
                    "기억이 쌓이는 자리도 둘로 갈라집니다.</p>"
                    f'<p><a class="btn" href="{ui.INSTALL_GUIDE_URL}" '
                    'target="_blank" rel="noopener">플러그인 설치 안내서 ↗</a></p>',
                ),
                (
                    "서버를 직접 띄우고 싶습니다.",
                    "<p>그 길도 있습니다. 차이는 하나입니다 — 서버를 회원님이 "
                    "직접 올리고 관리하느냐, 나무가 대신 맡느냐.</p>"
                    f'<p><a class="btn" href="{ui.SELFHOST_GUIDE_URL}" '
                    'target="_blank" rel="noopener">직접 서버 띄우기 안내서 ↗</a>'
                    "</p>",
                ),
                (
                    "그만두려면 어떻게 하나요?",
                    "<p>내 페이지에서 주소를 폐기하고, GitHub 설정에서 나무 앱의 "
                    "권한을 회수하시면 됩니다. 기억이 담긴 저장소는 회원님 "
                    "것이므로 그대로 남습니다.</p>"
                    '<p><a class="btn" href="/safety">안전 페이지에서 자세히 →</a>'
                    "</p>",
                ),
            ]
        )
        + '<div class="card card-accent" style="margin-top:30px">'
        "<h4>아직 안 되는 것</h4>"
        "<p class='muted' style='margin:0'>웹에서 기억을 고쳐 쓰는 것은 아직 "
        "안 됩니다(쪽지 떼기만 예외입니다). 접속 주소는 사람이 브라우저로 받아 "
        "손으로 붙이는 방식입니다.</p>"
        "</div>"
    )
    return ui.page(
        "자주 묻는 질문 — 나무 클라우드",
        body,
        current="/faq",
        cta="me" if logged_in else "start",
        description="요금·저장소·여러 AI 연결·그만두는 법 등 나무 클라우드에 "
        "대해 자주 묻는 질문.",
    )


# 경로 → 그리는 함수. web_auth가 라우트를 걸 때와 routing_server가 공개 경로를
# 열 때 **같은 목록**을 봐야 한 쪽만 늘어나는 사고가 없다.
PAGES = {
    "/": home_page,
    "/start": start_page,
    "/memory": memory_page,
    "/safety": safety_page,
    "/faq": faq_page,
}
