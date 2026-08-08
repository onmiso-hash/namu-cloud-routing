"""나무 클라우드 사이트의 공통 차림새 — 색·껍데기·부품 (namu-70).

**왜 web_auth.py에서 떼어냈나.** 공개 페이지(홈·시작하기·안전 …)는 로그인이
필요 없다. 그런데 껍데기가 web_auth.py 안에 있으면 공개 페이지를 그리는 모듈이
**로그인 모듈을 불러야** 한다 — 인증과 무관한 화면이 인증 코드에 의존하는
뒤집힌 구조다. 그래서 "어느 화면이든 쓰는 차림새"만 이 파일로 내렸다.
이 파일은 표준 모듈(html) 하나 말고는 아무 것도 import 하지 않는다 — 어느
쪽에서 불러도 순환이 생기지 않는 자리에 있어야 하기 때문이다.

**옛 안내서 HTML을 옮겨 오지 않는다.** `namu-agent/docs/*.html`은 색 감각(청록
계열)만 이어받고, 구조와 문구는 여기서 새로 짠다. 그 문서들에는 이미 폐기된
설명(전원 공용 열쇠, "그릇 두 개")이 남아 있어 그대로 옮기면 사이트가 첫날부터
거짓말을 한다.

**알림 상자만 인라인 스타일을 유지한다.** `notice()`가 만드는 조각은 화면에
박혀 나가기도 하지만 연결 시험(namu-69)에서는 **JSON으로 실려 나가** 자바스크립트가
그 자리에 심는다. 클래스로 바꾸면 ①그 조각을 심는 스크립트가
`box.className='namu-pop'`으로 클래스를 통째로 갈아 끼우는 순간 색이 사라지고
②조각만 떼어 보는 시험이 색을 확인할 방법이 없어진다. 스스로 색을 지고 다녀야
하는 조각이라 인라인이 맞다.
"""
import html

# ---------------------------------------------------------------------------
# 1. 디자인 값 — 색·모서리·그림자. 밝을 때와 어두울 때 한 쌍씩.
#
# 강조색은 안내서 6종이 이미 쓰고 있는 청록이다. 안내서를 보고 넘어온 사람이
# 같은 색을 만나야 "같은 서비스"로 읽힌다 — 여기서 새 색을 만들지 않는다.
# ---------------------------------------------------------------------------
_TOKENS_CSS = (
    ":root{"
    # color-scheme이 없으면 어두운 화면에서 입력칸·버튼만 하얗게 남는다.
    "color-scheme:light dark;"
    "--bg:#ffffff;--bg-soft:#f4f8f8;--bg-card:#ffffff;--bg-elev:#f8fbfb;"
    "--fg:#132320;--fg-soft:#5a6b68;--fg-faint:#849693;"
    "--accent:#0f766e;--accent-deep:#0b5750;--accent-soft:#e4f4f1;"
    "--accent-glow:rgba(15,118,110,.14);"
    "--on-accent:#ffffff;"
    "--border:#e2ebe9;--border-strong:#cddedb;"
    "--ok:#1a7f4b;--warn:#a15c00;--danger:#b3261e;"
    "--shadow:0 1px 2px rgba(15,40,36,.06),0 6px 20px -12px rgba(15,40,36,.22);"
    "--shadow-lg:0 2px 6px rgba(15,40,36,.07),0 18px 40px -22px rgba(15,40,36,.35);"
    "--radius:16px;--radius-sm:10px;"
    "--maxw:760px;--maxw-wide:1040px;"
    "}"
    "@media (prefers-color-scheme:dark){:root{"
    "--bg:#0e1513;--bg-soft:#131c19;--bg-card:#16201d;--bg-elev:#1a2522;"
    "--fg:#e8f0ed;--fg-soft:#a3b4b0;--fg-faint:#7b8e8a;"
    "--accent:#4fd6c6;--accent-deep:#7be6d8;--accent-soft:#17322e;"
    "--accent-glow:rgba(79,214,198,.12);"
    # 어두울 때 강조색이 밝아지므로 그 위에 얹는 글자는 반대로 어두워야 한다.
    "--on-accent:#07211d;"
    "--border:#243330;--border-strong:#31443f;"
    "--ok:#4fd18a;--warn:#e0a94e;--danger:#ef8b83;"
    "--shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px -12px rgba(0,0,0,.6);"
    "--shadow-lg:0 2px 6px rgba(0,0,0,.45),0 18px 40px -22px rgba(0,0,0,.7);"
    "}}"
)

# ---------------------------------------------------------------------------
# 2. 본문 기본기. 여기 있는 규칙은 전부 실측으로 필요해진 것들이다.
#  - viewport(page()에 있음): 없으면 모바일이 데스크톱 폭을 가정해 축소해 그린다.
#  - overflow-wrap/word-break: 접속 주소는 공백 없는 100자 한 덩어리라 줄바꿈
#    지점이 없다 — 그대로 두면 좁은 화면을 옆으로 밀어낸다.
# ---------------------------------------------------------------------------
_BASE_CSS = (
    "*{box-sizing:border-box;}"
    "html{scroll-behavior:smooth;}"
    "body{margin:0;background:var(--bg);color:var(--fg);line-height:1.75;"
    "font-size:16.5px;letter-spacing:-.003em;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Malgun Gothic',"
    "'Apple SD Gothic Neo',Roboto,Helvetica,Arial,sans-serif;"
    "-webkit-font-smoothing:antialiased;}"
    ".wrap{max-width:var(--maxw);margin:0 auto;padding:36px 20px 8px;}"
    ".wrap-wide{max-width:var(--maxw-wide);margin:0 auto;padding:0 20px;}"
    "h1{font-size:1.75rem;line-height:1.3;margin:0 0 .55em;letter-spacing:-.02em;}"
    "h2{font-size:1.3rem;line-height:1.4;margin:2em 0 .5em;letter-spacing:-.015em;}"
    "h3{font-size:1.05rem;margin:1.6em 0 .4em;letter-spacing:-.01em;}"
    "h4{font-size:1rem;margin:0 0 .3em;}"
    "p,li{overflow-wrap:break-word;word-break:break-word;}"
    "p{margin:0 0 1em;}"
    "a{color:var(--accent-deep);text-underline-offset:2px;}"
    "hr{border:0;border-top:1px solid var(--border);margin:2.4em 0;}"
    "small{font-size:.86rem;color:var(--fg-soft);}"
    "code{font-family:'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace;"
    "font-size:.87em;background:var(--accent-soft);color:var(--accent-deep);"
    "padding:.12em .42em;border-radius:5px;overflow-wrap:anywhere;}"
    "textarea,button,input,pre{max-width:100%;font-family:inherit;}"
    "pre{overflow-x:auto;background:var(--bg-soft);border:1px solid var(--border);"
    "padding:12px 14px;border-radius:var(--radius-sm);font-size:.85rem;}"
    "pre code{background:none;padding:0;color:inherit;}"
    "textarea,input[type=text]{background:var(--bg-card);color:var(--fg);"
    "border:1px solid var(--border-strong);border-radius:var(--radius-sm);"
    "padding:9px 11px;font-size:15px;}"
    "textarea:focus,input:focus,button:focus-visible,a:focus-visible,"
    "summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}"
    "details{border:1px solid var(--border);border-radius:var(--radius-sm);"
    "padding:0 16px;margin:10px 0;background:var(--bg-card);}"
    "details[open]{padding-bottom:12px;}"
    "summary{cursor:pointer;font-weight:600;padding:13px 0;list-style:none;"
    "display:flex;gap:9px;align-items:baseline;}"
    "summary::-webkit-details-marker{display:none;}"
    "summary::before{content:'▸';color:var(--accent);font-size:.85em;"
    "transition:transform .15s;}"
    "details[open] summary::before{transform:rotate(90deg);}"
    "table{width:100%;border-collapse:collapse;margin:1.1em 0;font-size:.92rem;}"
    "th,td{border:1px solid var(--border);padding:10px 12px;text-align:left;"
    "vertical-align:top;}"
    "th{background:var(--bg-soft);font-weight:700;}"
    "ul,ol{padding-left:1.3em;}"
    "li{margin:.3em 0;}"
    # 그 자리에서 바뀐 결과가 "방금 나타났다"는 것을 몸으로 알리는 짧은 등장 효과
    # (namu-69). 움직임을 줄여 달라고 설정한 사용자에게는 켜지 않는다.
    "@keyframes namu-pop{from{opacity:0;transform:translateY(-4px);}"
    "to{opacity:1;transform:none;}}"
    ".namu-pop{animation:namu-pop .25s ease-out;}"
    "@media (prefers-reduced-motion:reduce){.namu-pop{animation:none;}"
    "html{scroll-behavior:auto;}}"
)

# ---------------------------------------------------------------------------
# 3. 상단 메뉴 · 발
# ---------------------------------------------------------------------------
_CHROME_CSS = (
    # 배경을 두 번 적는 이유: color-mix를 모르는 브라우저는 그 줄만 건너뛰므로
    # 앞줄의 불투명한 배경이 남는다. 한 줄만 적으면 그런 브라우저에서 메뉴 줄이
    # 투명해져 본문 글자와 겹친다.
    ".topbar{position:sticky;top:0;z-index:20;background:var(--bg);"
    "background:color-mix(in srgb,var(--bg) 88%,transparent);"
    "backdrop-filter:saturate(180%) blur(12px);"
    "border-bottom:1px solid var(--border);}"
    ".topbar-in{max-width:var(--maxw-wide);margin:0 auto;padding:0 20px;"
    "display:flex;align-items:center;gap:18px;min-height:58px;}"
    ".brand{display:inline-flex;align-items:center;gap:7px;font-weight:800;"
    "color:var(--fg);text-decoration:none;font-size:1.02rem;letter-spacing:-.02em;"
    "white-space:nowrap;}"
    ".brand .leaf{font-size:1.15em;}"
    ".menu{display:flex;gap:2px;list-style:none;margin:0;padding:0;flex:1;"
    "overflow-x:auto;scrollbar-width:none;}"
    ".menu::-webkit-scrollbar{display:none;}"
    ".menu a{display:inline-block;padding:7px 11px;border-radius:8px;"
    "text-decoration:none;color:var(--fg-soft);font-size:.92rem;font-weight:600;"
    "white-space:nowrap;}"
    ".menu a:hover{background:var(--bg-soft);color:var(--fg);}"
    ".menu a.on{color:var(--accent-deep);background:var(--accent-soft);}"
    ".topbar .btn{padding:7px 14px;font-size:.9rem;}"
    "@media (max-width:720px){.topbar-in{flex-wrap:wrap;gap:8px;padding:8px 16px;}"
    ".menu{order:3;width:100%;flex:none;margin:0 -16px;padding:0 16px 6px;}"
    ".brand{flex:1;}}"
    ".sitefoot{border-top:1px solid var(--border);margin-top:4em;"
    "background:var(--bg-soft);padding:30px 20px 40px;}"
    ".sitefoot-in{max-width:var(--maxw-wide);margin:0 auto;display:flex;"
    "flex-wrap:wrap;gap:18px 40px;justify-content:space-between;}"
    ".sitefoot .col h4{font-size:.8rem;letter-spacing:.06em;"
    "text-transform:uppercase;color:var(--fg-faint);margin:0 0 8px;}"
    ".sitefoot ul{list-style:none;margin:0;padding:0;}"
    ".sitefoot li{margin:5px 0;}"
    ".sitefoot a{color:var(--fg-soft);text-decoration:none;font-size:.9rem;}"
    ".sitefoot a:hover{color:var(--accent-deep);text-decoration:underline;}"
    ".sitefoot .note{color:var(--fg-faint);font-size:.83rem;max-width:34ch;"
    "margin:0;}"
)

# ---------------------------------------------------------------------------
# 4. 부품 — 버튼·카드·걸음·알약. 흩어져 있던 인라인 style을 여기 한 곳에 모은다.
# ---------------------------------------------------------------------------
_COMPONENT_CSS = (
    ".btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;"
    "padding:10px 18px;font-size:15px;line-height:1.35;cursor:pointer;"
    "border-radius:11px;border:1px solid var(--border-strong);"
    "background:var(--bg-card);color:var(--fg);text-decoration:none;"
    "font-weight:600;transition:transform .12s,border-color .12s,box-shadow .12s;}"
    ".btn:hover{border-color:var(--accent);box-shadow:var(--shadow);}"
    ".btn:active{transform:translateY(1px);}"
    ".btn-primary{background:var(--accent);border-color:var(--accent);"
    "color:var(--on-accent);font-weight:700;box-shadow:var(--shadow);}"
    ".btn-primary:hover{background:var(--accent-deep);border-color:var(--accent-deep);"
    "box-shadow:var(--shadow-lg);}"
    ".btn-lg{padding:14px 26px;font-size:1.05rem;border-radius:13px;}"
    ".btn-danger{color:var(--danger);border-color:color-mix(in srgb,"
    "var(--danger) 40%,var(--border-strong));}"
    ".btn-row{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:16px 0;}"
    ".btn-row form{display:inline;margin:0;}"
    ".btn-note{color:var(--fg-soft);font-size:.88rem;margin:10px 0 0;}"
    ".card{background:var(--bg-card);border:1px solid var(--border);"
    "border-radius:var(--radius);padding:20px 22px;margin:16px 0;}"
    ".card>:first-child{margin-top:0;}"
    ".card>:last-child{margin-bottom:0;}"
    ".card-soft{background:var(--bg-soft);border-color:transparent;}"
    ".card-accent{background:var(--accent-soft);"
    "border-color:color-mix(in srgb,var(--accent) 28%,var(--border));}"
    # 저장소 고르기 목록 — 줄 전체가 눌리는 넓은 과녁이라야 손가락으로도 고른다.
    ".repo-list{list-style:none;margin:0;padding:0;}"
    ".repo-list li+li{border-top:1px solid var(--border);}"
    ".repo-list a{display:block;padding:13px 4px;text-decoration:none;"
    "color:var(--fg);font-weight:600;}"
    ".repo-list a::after{content:'→';float:right;color:var(--accent);}"
    ".repo-list a:hover{color:var(--accent-deep);}"
    ".grid{display:grid;gap:14px;margin:20px 0;}"
    "@media (min-width:640px){.grid-2{grid-template-columns:repeat(2,1fr);}"
    ".grid-3{grid-template-columns:repeat(3,1fr);}"
    ".grid-4{grid-template-columns:repeat(2,1fr);}}"
    "@media (min-width:900px){.grid-4{grid-template-columns:repeat(4,1fr);}}"
    ".eyebrow{display:inline-flex;align-items:center;gap:7px;font-size:.78rem;"
    "font-weight:700;letter-spacing:.04em;color:var(--accent-deep);"
    "background:var(--accent-soft);padding:6px 13px;border-radius:999px;}"
    ".pill{display:inline-block;font-size:.75rem;font-weight:700;padding:3px 10px;"
    "border-radius:999px;background:var(--accent);color:var(--on-accent);}"
    ".muted{color:var(--fg-soft);}"
    ".lead{font-size:1.08rem;color:var(--fg-soft);margin:0 0 1.2em;}"
    # 번호 붙은 걸음 — 세로로 이어지는 선 위에 번호가 놓인다.
    ".steps{list-style:none;margin:22px 0;padding:0;position:relative;}"
    ".steps>li{display:flex;gap:16px;padding:0 0 22px;position:relative;}"
    ".steps>li:last-child{padding-bottom:0;}"
    ".steps>li::before{content:'';position:absolute;left:16px;top:34px;bottom:0;"
    "width:2px;background:var(--border);}"
    ".steps>li:last-child::before{display:none;}"
    ".steps .num{flex:none;width:34px;height:34px;border-radius:50%;"
    "background:var(--accent);color:var(--on-accent);display:flex;"
    "align-items:center;justify-content:center;font-weight:800;font-size:.95rem;"
    "box-shadow:0 0 0 4px var(--bg);z-index:1;}"
    ".steps .txt{flex:1;min-width:0;padding-top:3px;}"
    ".steps .txt h4{margin:0 0 4px;}"
    ".steps .txt p{margin:0 0 .4em;color:var(--fg-soft);}"
    # 진행 표시(●─●─○─○)
    ".stepper{display:flex;align-items:center;margin:0;padding:0;list-style:none;}"
    ".stepper .dot{width:11px;height:11px;border-radius:50%;"
    "background:var(--border-strong);}"
    ".stepper .dot.on{background:var(--accent);}"
    ".stepper .bar{width:24px;height:2px;background:var(--border-strong);}"
    ".stepper-label{margin:8px 0 22px;color:var(--fg-soft);font-size:.9rem;"
    "font-weight:700;letter-spacing:.01em;}"
)

# ---------------------------------------------------------------------------
# 5. 공개 페이지 전용 — 첫인상 칸, 흐름 그림, 갈림길.
#
# 흐름 그림을 SVG가 아니라 HTML 상자로 만드는 이유: 글자가 들어가는 그림이라
# SVG로 그리면 화면 낭독기가 읽지 못하고 좁은 화면에서 줄바꿈도 안 된다.
# 상자로 만들면 그냥 글이라 둘 다 공짜로 해결된다.
# ---------------------------------------------------------------------------
_PUBLIC_CSS = (
    ".hero{position:relative;overflow:hidden;border-bottom:1px solid var(--border);"
    "background:"
    "radial-gradient(80% 120% at 78% 8%,var(--accent-glow),transparent 62%),"
    "linear-gradient(180deg,var(--bg-soft),var(--bg));}"
    ".hero-in{max-width:var(--maxw-wide);margin:0 auto;padding:64px 20px 60px;"
    "display:grid;gap:34px;align-items:center;}"
    "@media (min-width:880px){.hero-in{grid-template-columns:1.05fr .95fr;"
    "padding:86px 20px 78px;}}"
    ".hero h1{font-size:clamp(2rem,5.2vw,2.9rem);line-height:1.2;"
    "letter-spacing:-.032em;margin:18px 0 14px;}"
    ".hero .lead{font-size:1.12rem;max-width:32ch;line-height:1.7;}"
    ".hero-cta{display:flex;flex-wrap:wrap;gap:11px;align-items:center;"
    "margin-top:26px;}"
    ".hero-art{display:flex;justify-content:center;}"
    ".hero-art svg{width:100%;max-width:400px;height:auto;}"
    # 첫인상 그림의 색. 그림 파일 안이 아니라 여기서 칠하므로 어두운 화면도
    # 따로 손볼 것이 없다(색 변수가 그대로 따라온다).
    ".ha-shadow{fill:var(--accent);opacity:.09;}"
    ".ha-sheet rect{fill:var(--bg-card);stroke:var(--border-strong);"
    "stroke-width:1.5;}"
    ".ha-line rect{fill:var(--fg-soft);opacity:.45;}"
    ".ha-mark circle{fill:var(--accent);opacity:.75;}"
    ".ha-trunk{stroke:var(--accent);stroke-width:3;stroke-linecap:round;"
    "opacity:.5;fill:none;}"
    ".ha-crown circle{fill:var(--accent);opacity:.9;}"
    ".ha-crown circle.soft{opacity:.6;}"
    ".sec{padding:56px 0 8px;}"
    ".sec-head{max-width:34em;margin:0 0 26px;}"
    ".sec-head h2{margin:8px 0 10px;font-size:clamp(1.35rem,3vw,1.75rem);}"
    ".sec-head p{color:var(--fg-soft);margin:0;font-size:1.02rem;}"
    ".band{background:var(--bg-soft);border-top:1px solid var(--border);"
    "border-bottom:1px solid var(--border);}"
    # 흐름 그림 — 내 AI → 나무 → 내 저장소
    ".flow{display:grid;gap:12px;align-items:stretch;margin:8px 0 4px;}"
    "@media (min-width:760px){.flow{grid-template-columns:1fr auto 1fr auto 1fr;}}"
    ".flow-node{background:var(--bg-card);border:1px solid var(--border);"
    "border-radius:var(--radius);padding:18px 18px 20px;text-align:center;"
    "box-shadow:var(--shadow);}"
    ".flow-node .ico{font-size:1.7rem;line-height:1;}"
    ".flow-node h4{margin:10px 0 5px;font-size:1rem;}"
    ".flow-node p{margin:0;font-size:.88rem;color:var(--fg-soft);}"
    ".flow-node.is-origin{border-color:var(--accent);"
    "box-shadow:0 0 0 3px var(--accent-glow),var(--shadow);}"
    ".flow-arrow{display:flex;align-items:center;justify-content:center;"
    "color:var(--accent);font-size:1.3rem;font-weight:700;}"
    "@media (max-width:759px){.flow-arrow{transform:rotate(90deg);}}"
    ".flow-cap{color:var(--fg-soft);font-size:.9rem;margin:16px 0 0;}"
    # 갈림길 — 이 사이트가 맞는 사람인지 가르는 두 칸
    ".fork{display:grid;gap:14px;margin:22px 0;}"
    "@media (min-width:700px){.fork{grid-template-columns:1fr 1fr;}}"
    ".fork .opt{border:2px solid var(--border);border-radius:var(--radius);"
    "padding:20px;background:var(--bg-card);}"
    ".fork .opt.here{border-color:var(--accent);background:var(--accent-soft);}"
    ".fork .opt h4{margin:9px 0 6px;font-size:1.05rem;}"
    ".fork .opt p{margin:0 0 12px;font-size:.93rem;color:var(--fg-soft);}"
    ".fork .opt .tag{font-size:.74rem;font-weight:800;letter-spacing:.05em;"
    "color:var(--fg-faint);}"
    ".fork .opt.here .tag{color:var(--accent-deep);}"
    # 눈에 띄어야 하는 한 줄(원본은 회원님 것이다 등)
    ".claim{display:flex;gap:12px;align-items:flex-start;padding:15px 17px;"
    "border-radius:var(--radius-sm);background:var(--bg-card);"
    "border:1px solid var(--border);}"
    ".claim .ic{flex:none;font-size:1.15rem;line-height:1.5;}"
    ".claim p{margin:0;font-size:.94rem;}"
    ".claim b{display:block;margin-bottom:2px;}"
    # 스르륵 등장 — 자바스크립트가 켜 준다. 스크립트가 없거나 실패하면
    # 아래 no-js 규칙에 따라 처음부터 보이는 상태로 남는다(내용이 사라지지 않는다).
    ".reveal{opacity:1;}"
    "html.js-reveal .reveal{opacity:0;transform:translateY(14px);"
    "transition:opacity .5s ease-out,transform .5s ease-out;}"
    "html.js-reveal .reveal.seen{opacity:1;transform:none;}"
    "@media (prefers-reduced-motion:reduce){html.js-reveal .reveal{opacity:1;"
    "transform:none;transition:none;}}"
)

# ---------------------------------------------------------------------------
# 5b. AI 안내원 말풍선 (namu-ai-guide 6단계)
#
# 설계서 8-2절이 정한 규칙 그대로다 — 바깥 파일을 쓰지 않고, 색은 위 변수만
# 쓰고(그래서 어두운 화면을 따로 손보지 않는다), 좁은 화면에서는 폭을 채운다.
# ---------------------------------------------------------------------------
_ASK_CSS = (
    # 눈으로는 안 보이고 화면 낭독기에는 읽히는 글자(동그란 단추의 이름).
    ".ask-sr{position:absolute;width:1px;height:1px;overflow:hidden;"
    "clip:rect(0 0 0 0);white-space:nowrap;}"
    ".ask{position:fixed;right:18px;bottom:18px;z-index:60;display:flex;"
    "flex-direction:column;align-items:flex-end;gap:10px;}"
    ".ask-fab{width:54px;height:54px;border-radius:50%;cursor:pointer;"
    "border:1px solid var(--accent);background:var(--accent);"
    "color:var(--on-accent);font-size:1.35rem;box-shadow:var(--shadow-lg);"
    "display:flex;align-items:center;justify-content:center;}"
    ".ask-fab:hover{background:var(--accent-deep);}"
    ".ask.on .ask-fab{display:none;}"
    ".ask-panel{display:flex;flex-direction:column;width:360px;"
    "max-width:calc(100vw - 36px);height:min(560px,calc(100vh - 110px));"
    "background:var(--bg-card);border:1px solid var(--border-strong);"
    "border-radius:var(--radius);box-shadow:var(--shadow-lg);overflow:hidden;}"
    ".ask-panel[hidden]{display:none;}"
    ".ask-head{display:flex;align-items:center;gap:8px;padding:10px 8px 10px 15px;"
    "border-bottom:1px solid var(--border);background:var(--bg-elev);}"
    ".ask-head b{flex:1;font-size:.95rem;}"
    ".ask-x{border:0;background:none;color:var(--fg-soft);font-size:.95rem;"
    "cursor:pointer;padding:6px 10px;border-radius:8px;}"
    ".ask-x:hover{background:var(--bg-soft);color:var(--fg);}"
    ".ask-log{flex:1;overflow-y:auto;padding:14px 15px;display:flex;"
    "flex-direction:column;gap:10px;}"
    ".ask-msg{max-width:92%;padding:10px 13px;border-radius:13px;"
    "font-size:.93rem;line-height:1.65;}"
    # 답에는 줄바꿈이 들어온다. HTML로 해석하지 않고 글자로만 넣으므로(설계서
    # 10-2절) 줄바꿈을 살리는 일은 <br>이 아니라 이 한 줄이 한다.
    ".ask-msg p{margin:0;white-space:pre-wrap;overflow-wrap:break-word;}"
    ".ask-bot{background:var(--bg-soft);border:1px solid var(--border);"
    "border-bottom-left-radius:4px;align-self:flex-start;}"
    ".ask-me{background:var(--accent);color:var(--on-accent);"
    "border-bottom-right-radius:4px;align-self:flex-end;}"
    ".ask-warn{background:var(--bg-soft);border:1px solid var(--warn);"
    "border-left-width:4px;align-self:flex-start;}"
    ".ask-wait{color:var(--fg-faint);}"
    ".ask-src{margin-top:7px;font-size:.83rem;color:var(--fg-soft);}"
    ".ask-src a{color:var(--accent-deep);}"
    ".ask-tips{display:flex;flex-wrap:wrap;gap:7px;}"
    ".ask-tip{border:1px solid var(--border-strong);background:var(--bg-card);"
    "color:var(--accent-deep);border-radius:999px;padding:6px 12px;"
    "font-size:.86rem;font-weight:600;cursor:pointer;}"
    ".ask-tip:hover{border-color:var(--accent);background:var(--accent-soft);}"
    ".ask-form{display:flex;gap:8px;padding:10px 12px 0;}"
    ".ask-form input{flex:1;min-width:0;}"
    ".ask-form .btn{padding:9px 15px;font-size:.92rem;}"
    # 고지 줄. 접히지 않고 늘 여기 있다(설계서 10-1절).
    ".ask-note{margin:9px 13px 12px;font-size:.76rem;line-height:1.6;"
    "color:var(--fg-soft);}"
    ".ask-note a{color:var(--accent-deep);white-space:nowrap;}"
    # 좁은 화면에서는 거의 꽉 채운다(설계서 8-2절).
    "@media (max-width:520px){.ask{right:12px;left:12px;bottom:12px;}"
    ".ask-panel{width:100%;max-width:100%;height:min(80vh,calc(100vh - 84px));}}"
)

SITE_CSS = (
    _TOKENS_CSS + _BASE_CSS + _CHROME_CSS + _COMPONENT_CSS + _PUBLIC_CSS + _ASK_CSS
)


# ---------------------------------------------------------------------------
# 6. 사이트 지도 — 상단 메뉴와 발이 같은 목록을 쓴다.
#
# 한 곳에만 적는 이유: 두 곳에 적으면 메뉴를 늘릴 때 한쪽만 고쳐진다(이
# 프로젝트에서 반복된 사고). 여기 없는 공개 경로는 만들지 않는다 —
# routing_server의 공개 경로 목록과 이 목록이 정확히 짝을 이뤄야 한다.
# ---------------------------------------------------------------------------
MENU = (
    ("/", "홈"),
    ("/start", "시작하기"),
    ("/memory", "무엇을 기억하나"),
    ("/safety", "안전"),
    ("/faq", "자주 묻는 질문"),
)

PUBLIC_PATHS = tuple(path for path, _label in MENU)

GITHUB_URL = "https://github.com/onmiso-hash/namu-agent"

# 안내서는 저장소의 마크다운이 아니라 **펴낸 안내서 사이트**를 가리킨다.
# 저장소 쪽 `docs/*.md`는 namu-74에서 "이 문서는 옮겨졌습니다" 표지판만 남았다 —
# 그리로 보내면 방문자가 한 번 더 눌러야 진짜 안내서에 닿는다.
GUIDE_SITE = "https://onmiso-hash.github.io/namu-agent/docs"
GUIDE_URL = f"{GUIDE_SITE}/index.html"
INSTALL_GUIDE_URL = f"{GUIDE_SITE}/install_guide.html"
SELFHOST_GUIDE_URL = f"{GUIDE_SITE}/remote_mcp_guide.html"


def topbar(current: str = "", cta: str = "me") -> str:
    """모든 화면 맨 위의 메뉴 줄.

    `cta`는 오른쪽 끝 버튼이다 — 공개 페이지에서는 `start`(가입 유도), 로그인
    뒤 화면에서는 `me`(내 페이지로 돌아가기)가 맞다.
    """
    items = "".join(
        '<li><a href="%s"%s>%s</a></li>'
        % (path, ' class="on"' if path == current else "", html.escape(label))
        for path, label in MENU
    )
    # 안내서로 돌아가는 길. 안내서 쪽 머리줄에는 '나무 클라우드' 버튼이 늘 있는데
    # 이쪽에는 돌아갈 문이 없어 왕복이 한쪽만 열려 있었다(꼬리말에만 있고, 꼬리말은
    # 끝까지 내려야 보인다). **MENU에 넣지 않는다** — 그 튜플은 화면 메뉴이면서
    # 동시에 `PUBLIC_PATHS`(로그인 없이 열어 주는 경로 목록)의 원본이라, 바깥
    # 주소를 끼우면 문 목록에 사이트 밖 주소가 섞인다.
    items += (
        f'<li><a href="{GUIDE_URL}" target="_blank" rel="noopener">나무 안내서 ↗</a></li>'
    )
    if cta == "start":
        button = '<a class="btn btn-primary" href="/auth/github/login">시작하기</a>'
    elif cta == "none":
        button = ""
    else:
        button = '<a class="btn" href="/auth/me">내 페이지</a>'
    return (
        '<header class="topbar"><div class="topbar-in">'
        '<a class="brand" href="/"><span class="leaf">🌳</span>나무 클라우드</a>'
        f'<ul class="menu">{items}</ul>{button}'
        "</div></header>"
    )


def footer() -> str:
    """모든 화면 맨 아래. 사이트 안의 페이지가 먼저, 바깥 문서가 나중이다."""
    pages = "".join(
        f'<li><a href="{path}">{html.escape(label)}</a></li>' for path, label in MENU
    )
    return (
        '<footer class="sitefoot"><div class="sitefoot-in">'
        '<div class="col">'
        '<h4>둘러보기</h4>'
        f"<ul>{pages}</ul>"
        "</div>"
        '<div class="col"><h4>터미널에서 쓰기</h4><ul>'
        f'<li><a href="{INSTALL_GUIDE_URL}" target="_blank" rel="noopener">'
        "플러그인 설치 안내서 ↗</a></li>"
        f'<li><a href="{SELFHOST_GUIDE_URL}" target="_blank" rel="noopener">'
        "직접 서버 운영하기 ↗</a></li>"
        f'<li><a href="{GUIDE_URL}" target="_blank" rel="noopener">'
        "나무 안내서 ↗</a></li>"
        f'<li><a href="{GITHUB_URL}" target="_blank" rel="noopener">'
        "GitHub 저장소 ↗</a></li>"
        "</ul></div>"
        '<div class="col">'
        "<h4>나무 클라우드</h4>"
        '<p class="note">기억의 원본은 회원님 GitHub 저장소에 있습니다. '
        "이 서버가 가진 것은 그 사본뿐이고, 언제든 연결을 끊을 수 있습니다.</p>"
        "</div>"
        "</div></footer>"
    )


# 스르륵 등장 — 외부 스크립트 없음. 자바스크립트가 있을 때만 감췄다가 보여주므로,
# 스크립트가 막힌 브라우저에서는 처음부터 다 보인다(내용이 사라지지 않는다).
_REVEAL_SCRIPT = (
    "<script>"
    "(function(){"
    "if(!window.IntersectionObserver)return;"
    "var r=document.documentElement;r.className+=' js-reveal';"
    "var io=new IntersectionObserver(function(es){"
    "es.forEach(function(e){if(e.isIntersecting){e.target.classList.add('seen');"
    "io.unobserve(e.target);}});},{rootMargin:'0px 0px -8% 0px'});"
    "document.querySelectorAll('.reveal').forEach(function(el){io.observe(el);});"
    "})();"
    "</script>"
)

# 이름표 아이콘. 바깥 파일을 받아오지 않도록 data URI로 심는다 — 이것이 없으면
# 브라우저가 /favicon.ico를 찾아 나서고, 그 요청은 우리 MCP 쪽으로 흘러간다.
_FAVICON = (
    '<link rel="icon" href="data:image/svg+xml,'
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
    "%3Ctext y='.9em' font-size='90'%3E%F0%9F%8C%B3%3C/text%3E%3C/svg%3E\">"
)


# ---------------------------------------------------------------------------
# 6b. AI 안내원 말풍선 — 화면 오른쪽 아래 (설계서 8절)
# ---------------------------------------------------------------------------
ASK_NOTICE = (
    "이 창의 질문은 Google AI로 보내집니다. 무료 등급이라 Google이 학습에 쓸 수 "
    "있고 사람이 읽을 수도 있습니다."
)
"""말풍선에 늘 보이는 고지(설계서 10-1절).

**이 문구를 떼는 것은 코드 수정이 아니라 약관 변경이다.** 안전 화면에
"원본은 회원님 것"이라고 써 둔 사이트가, 여기 친 글이 남의 학습에 쓰이는 것을
말하지 않으면 그 약속이 거짓이 된다. 안전 화면(`pages.safety_page`)에도 같은
내용이 문단으로 있고, `tests/test_ui.py`·`tests/test_pages.py`가 둘 다 지킨다.
"""

ASK_NOTICE_STRONG = "개인정보나 접속 열쇠는 적지 마세요."

# 처음 펼쳤을 때 눌러 볼 수 있는 질문. 빈 칸만 있으면 무엇을 물어야 할지 몰라
# 그냥 닫는다(설계서 8-1절).
_ASK_TIPS = ("나무가 뭔가요?", "무료인가요?", "어떻게 시작하나요?")

_ASK_SCRIPT = (
    "<script>"
    "(function(){"
    "var root=document.getElementById('namu-ask');"
    # fetch가 없는 옛 브라우저에서는 눌러도 답이 안 오므로 단추를 아예 치운다.
    "if(!root||!window.fetch){if(root)root.parentNode.removeChild(root);return;}"
    "var fab=root.querySelector('.ask-fab');"
    "var panel=root.querySelector('.ask-panel');"
    "var closeBtn=root.querySelector('.ask-x');"
    "var log=root.querySelector('.ask-log');"
    "var form=root.querySelector('.ask-form');"
    "var input=form.querySelector('input');"
    # 대화는 서버에 저장하지 않는다(설계서 14절). 이 배열이 전부이고, 창을
    # 닫거나 화면을 옮기면 그대로 사라진다.
    "var hist=[];var busy=false;"
    # 보내기 전에 화면에서 거르는 열쇠 모양(설계서 10-2절). 서버로 넘어간
    # 뒤에는 이미 늦다 — 그때는 이미 AI 회사로 갈 길에 올라 있다.
    "var KEYLIKE=/(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"
    "|sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,})/;"
    "function open(){panel.hidden=false;root.className='ask on';"
    "fab.setAttribute('aria-expanded','true');input.focus();}"
    "function shut(){panel.hidden=true;root.className='ask';"
    "fab.setAttribute('aria-expanded','false');fab.focus();}"
    "function bubble(cls,text,sources){"
    "var d=document.createElement('div');d.className='ask-msg '+cls;"
    # 답은 글자로만 넣는다 — textContent라서 태그가 와도 태그로 해석되지 않는다.
    "var p=document.createElement('p');p.textContent=text;d.appendChild(p);"
    "if(sources&&sources.length){"
    "var s=document.createElement('p');s.className='ask-src';"
    "s.appendChild(document.createTextNode('근거 '));"
    "sources.forEach(function(x,i){"
    # 주소는 우리 화면이거나 https만 건다. 서버가 우리 목록에서만 고르지만,
    # 화면에서도 한 번 더 본다.
    "var u=String(x.url||'');"
    "if(u.charAt(0)!=='/'&&u.indexOf('https://')!==0)return;"
    "if(i)s.appendChild(document.createTextNode(' · '));"
    "var a=document.createElement('a');a.href=u;"
    "a.textContent=String(x.label||u);"
    "if(u.charAt(0)!=='/'){a.target='_blank';a.rel='noopener';}"
    "s.appendChild(a);});"
    "if(s.querySelector('a'))d.appendChild(s);}"
    "log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}"
    "function send(q){"
    "if(!q||busy)return;"
    "if(KEYLIKE.test(q)){bubble('ask-warn','열쇠나 비밀번호처럼 보이는 글자가 "
    "있어 보내지 않았습니다. 그 부분을 지우고 다시 물어봐 주세요.');return;}"
    "input.value='';bubble('ask-me',q);"
    "busy=true;var wait=bubble('ask-bot ask-wait','…');"
    "fetch('/auth/ask',{method:'POST',credentials:'same-origin',"
    "headers:{'Content-Type':'application/json'},"
    "body:JSON.stringify({question:q,history:hist})})"
    ".then(function(r){return r.json();})"
    ".then(function(d){"
    "if(wait.parentNode)wait.parentNode.removeChild(wait);"
    "bubble('ask-bot',d.text||'지금은 답할 수 없습니다.',d.sources);"
    "if(d.ok&&d.text){hist.push({q:q,a:d.text});if(hist.length>3)hist.shift();}})"
    ".catch(function(){"
    "if(wait.parentNode)wait.parentNode.removeChild(wait);"
    "bubble('ask-bot','지금은 답할 수 없습니다. 잠시 뒤 다시 물어봐 주시고, "
    "급하시면 나무 안내서를 보세요.');})"
    ".then(function(){busy=false;input.focus();});}"
    "fab.addEventListener('click',open);"
    "closeBtn.addEventListener('click',shut);"
    "form.addEventListener('submit',function(e){e.preventDefault();"
    "send(input.value.trim());});"
    "root.addEventListener('keydown',function(e){"
    "if(e.key==='Escape'&&!panel.hidden)shut();});"
    "log.addEventListener('click',function(e){"
    "if(e.target.classList.contains('ask-tip'))send(e.target.textContent);});"
    "})();"
    "</script>"
)


def ask_widget() -> str:
    """오른쪽 아래 말풍선 한 벌. 열쇠가 없으면 빈 글자를 돌려준다.

    **`ask` 모듈을 함수 안에서 부르는 이유** — 이 파일은 표준 모듈 하나 말고는
    아무 것도 불러오지 않는 자리에 있다(파일 첫머리). 위에서 `import ask`를 하면
    `ask → ask_corpus → pages → ui`로 도는 길이 생긴다. `ask_corpus`가 `pages`를
    함수 안에서 부르는 것과 같은 방식으로 여기서도 미룬다.

    **열쇠가 없으면 단추를 아예 안 그린다**(설계서 11절). 눌러도 답이 안 오는
    단추를 보여 주느니 없는 편이 낫고, 배포 순서가 어긋나도 사고가 나지 않는다.
    """
    import ask

    if not ask.is_enabled():
        return ""

    tips = "".join(
        f'<button type="button" class="ask-tip">{html.escape(t)}</button>'
        for t in _ASK_TIPS
    )
    return (
        # 자바스크립트가 꺼져 있으면 단추를 그리지 않는다(설계서 8-2절) —
        # 스크립트가 없으면 눌러도 아무 일이 일어나지 않기 때문이다.
        "<noscript><style>#namu-ask{display:none;}</style></noscript>"
        '<div class="ask" id="namu-ask">'
        '<button type="button" class="ask-fab" aria-expanded="false"'
        ' aria-controls="namu-ask-panel">'
        '<span aria-hidden="true">💬</span>'
        '<span class="ask-sr">나무에게 물어보기</span></button>'
        '<section class="ask-panel" id="namu-ask-panel" hidden'
        ' aria-label="나무에게 물어보기">'
        '<div class="ask-head"><b>🌳 나무에게 물어보기</b>'
        '<button type="button" class="ask-x" aria-label="닫기">✕</button></div>'
        # role="log" + aria-live: 답이 도착하면 화면을 보지 않는 사용자에게도
        # 읽힌다(설계서 8-2절).
        '<div class="ask-log" role="log" aria-live="polite">'
        '<div class="ask-msg ask-bot"><p>안녕하세요. 나무 클라우드에 대해 '
        "궁금한 것을 물어보세요.</p></div>"
        f'<div class="ask-tips">{tips}</div>'
        "</div>"
        '<form class="ask-form">'
        '<input type="text" maxlength="500" autocomplete="off"'
        ' aria-label="질문" placeholder="궁금한 것을 물어보세요">'
        '<button type="submit" class="btn btn-primary">보내기</button>'
        "</form>"
        f'<p class="ask-note">ⓘ {ASK_NOTICE} <b>{ASK_NOTICE_STRONG}</b> '
        '<a href="/safety">자세히</a></p>'
        "</section></div>" + _ASK_SCRIPT
    )


def page(
    title: str,
    body_html: str,
    *,
    current: str = "",
    cta: str = "me",
    description: str = "",
    reveal: bool = False,
    raw_body: bool = False,
    ask: bool = True,
) -> str:
    """모든 화면의 공통 껍데기. 외부 CSS/웹폰트/CDN 없이 인라인 <style> 하나다.

    `raw_body=True`는 첫인상 칸처럼 **화면 폭을 꽉 채우는** 공개 페이지용이다 —
    기본값은 읽기 좋은 폭(760px)으로 감싼다.

    `ask=False`면 AI 안내원 말풍선을 그 화면에만 빼 준다. 기본값이 참인 것은
    사용자 결정이다 — "모든 화면 구석에 말풍선"(설계서 2절). 화면 5장을 각각
    손대지 않고 여기 한 곳에서 붙는다.
    """
    meta = (
        f'<meta name="description" content="{html.escape(description)}">'
        if description
        else ""
    )
    inner = body_html if raw_body else f'<div class="wrap">{body_html}</div>'
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>{meta}{_FAVICON}"
        f"<style>{SITE_CSS}</style></head>"
        f"<body>{topbar(current, cta)}{inner}{footer()}"
        f"{_REVEAL_SCRIPT if reveal else ''}"
        f"{ask_widget() if ask else ''}</body></html>"
    )


# ---------------------------------------------------------------------------
# 7. 알림 상자 — 스스로 색을 지고 다니는 조각(파일 첫머리 설명 참고).
# ---------------------------------------------------------------------------
_NOTICE_TONES = {
    "info": ("#2a6fdb", "rgba(42,111,219,0.10)", "ℹ️"),
    "good": ("#1a7f37", "rgba(26,127,55,0.12)", "✅"),
    "warn": ("#b06000", "rgba(176,96,0,0.12)", "⚠️"),
    "bad": ("#b00020", "rgba(176,0,32,0.12)", "⛔"),
    "wait": ("#6b7280", "rgba(107,114,128,0.12)", "⏳"),
}


def notice(
    text_html: str, *, tone: str = "info", attrs: str = "", style_extra: str = ""
) -> str:
    """결과 알림 상자. 아이콘 + 왼쪽 굵은 선 + 옅은 배경으로 본문과 확실히 구분한다.

    `role="status"`를 붙이는 이유: 이 상자는 사용자가 버튼을 누른 결과가 화면에
    나타나는 자리다. 화면을 보지 않는 사용자(스크린리더)에게도 그 등장이 읽혀야
    "눌렸는지 모르겠다"가 생기지 않는다.

    `attrs`는 여는 태그에 그대로 붙일 속성(예: `id`), `style_extra`는 같은 style
    안에 **앞에 얹을** 선언이다(예: 진행 표시 상자의 `display:none`). style을
    호출부가 따로 붙이지 않고 이 칸으로 받는 이유: 한 태그에 style 속성이 두 개
    있으면 브라우저가 뒤엣것을 통째로 버려 색이 사라진다.
    """
    color, tint, icon = _NOTICE_TONES.get(tone, _NOTICE_TONES["info"])
    head = f'<p role="status" {attrs} ' if attrs else '<p role="status" '
    return (
        f'{head}style="{style_extra}border-left:5px solid {color};background:{tint};'
        'padding:12px 14px;margin:16px 0;border-radius:0 6px 6px 0;">'
        f'<span aria-hidden="true">{icon}</span> {text_html}</p>'
    )


# ---------------------------------------------------------------------------
# 8. 진행 표시 — 처음 한 번 거치는 길(1~4단계)에서 "지금 몇 번째인지"를 알린다.
# ---------------------------------------------------------------------------
def stepper(current: int, total: int = 4, label: str = "") -> str:
    """`●─●─○─○` 그림 + 같은 뜻의 글자 한 줄.

    그림만 내지 않는 이유: 동그라미는 화면 낭독기에 아무것도 읽히지 않는다.
    `2단계 / 4단계 — 기억 저장소`를 글자로 함께 적어야 눈으로 보지 않는
    사용자도 자기가 어디쯤인지 안다(그림 쪽은 `aria-hidden`으로 감춘다).
    """
    dots = []
    for n in range(1, total + 1):
        if n > 1:
            dots.append('<li class="bar"></li>')
        on = " on" if n <= current else ""
        dots.append(f'<li class="dot{on}"></li>')
    text = f"{current}단계 / {total}단계"
    if label:
        text += f" — {html.escape(label)}"
    return (
        f'<ul class="stepper" aria-hidden="true">{"".join(dots)}</ul>'
        f'<p class="stepper-label">{text}</p>'
    )


# ---------------------------------------------------------------------------
# 9. 공개 페이지가 함께 쓰는 조각들
# ---------------------------------------------------------------------------
def section(
    body_html: str, *, eyebrow: str = "", title: str = "", sub: str = "", band: bool = False
) -> str:
    """제목 한 벌 + 본문 한 덩어리. 페이지마다 제목 모양을 새로 짜지 않게 한다."""
    head = ""
    if eyebrow or title or sub:
        head = '<div class="sec-head">'
        if eyebrow:
            head += f'<span class="eyebrow">{html.escape(eyebrow)}</span>'
        if title:
            head += f"<h2>{html.escape(title)}</h2>"
        if sub:
            head += f"<p>{sub}</p>"
        head += "</div>"
    inner = f'<section class="sec reveal">{head}{body_html}</section>'
    if band:
        return f'<div class="band"><div class="wrap-wide">{inner}</div></div>'
    return f'<div class="wrap-wide">{inner}</div>'


def steps(items: "list[tuple[str, str]]") -> str:
    """번호 붙은 걸음 목록 — `(제목, 설명 HTML)` 차례."""
    lis = []
    for n, (title, text) in enumerate(items, start=1):
        lis.append(
            f'<li><span class="num" aria-hidden="true">{n}</span>'
            f'<div class="txt"><h4>{html.escape(title)}</h4>{text}</div></li>'
        )
    return f'<ol class="steps">{"".join(lis)}</ol>'


def faq(items: "list[tuple[str, str]]") -> str:
    """접이식 질문 목록 — `(질문, 답 HTML)` 차례."""
    return "".join(
        f"<details><summary>{html.escape(q)}</summary>{a}</details>" for q, a in items
    )
