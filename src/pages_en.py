"""나무 클라우드 공개 페이지의 영어판 — Home·Get started·What it remembers·
Safety·FAQ (namu-83).

**왜 문구 사전이 아니라 파일 한 벌인가.** `pages.py`는 문구와 화면 뼈대가 한
덩어리로 짜여 있다(`<p class="lead">` 안에 문장이 바로 들어 있는 식). 문구만
사전으로 빼내려면 그 파일 682줄을 통째로 다시 써야 하고, 그동안 잘 돌아가던
한국어 화면이 함께 위태로워진다. 그래서 한국어판은 **한 줄도 건드리지 않고**
같은 부품(`ui.section`·`ui.steps`·`ui.faq`)으로 조립한 영어판을 옆에 두었다.

**대신 이 파일은 두 벌 관리를 진다.** `pages.py`의 문구를 고치면 여기도 함께
고쳐야 한다. 색·글꼴·여백 같은 차림새는 `ui.py` 한 곳에서 오므로 갈라지지
않지만, **문장은 갈라진다** — 한국어 쪽만 고치고 여기를 잊으면 영어 화면이
조용히 옛말을 하게 된다.

**AI 안내원(오른쪽 아래 말풍선)을 켜지 않는다.** `ask.py`의 지시문에 "한국어로
씁니다"가 박혀 있어(같은 파일 4번 항목), 영어로 물어도 한국어 답이 돌아온다.
영어 방문자에게 읽지 못하는 답을 주느니 단추를 내지 않는 편이 낫다. 그 지시문이
질문의 언어를 따라가도록 바뀌면 이 파일의 `ask=False`를 함께 걷어내면 된다.

**로그인 뒤 화면은 아직 한국어뿐이다.** 이 파일의 링크가 `/auth/…`로 넘어가는
순간 화면이 한국어로 바뀐다. 숨기지 않고 `start_page`에 그 사실을 적어 두었다 —
적지 않으면 방문자가 가입 도중에 막힌 채로 이유를 모른다.
"""
import pages
import ui

# 접속 주소의 생김새. 한국어판(`pages._URL_SHAPE`)과 같은 모양이되 자리표시자만
# 영어다 — 진짜 열쇠가 아니라는 것이 한눈에 보여야 하는 자리다.
_URL_SHAPE = "https://namu-cloud.onnamu.kr/mcp/&lt;my-key&gt;?client=claude"


def _flow_diagram() -> str:
    """기억이 실제로 어디에 있는지 — 세 칸과 화살표.

    한국어판과 같은 이유로 이 그림이 사이트에서 가장 중요한 설명이다. "AI가
    기억한다"는 말을 들은 사람이 가장 먼저 하는 걱정이 '내 이야기가 어디에
    쌓이느냐'이기 때문이다.
    """
    return (
        '<div class="flow">'
        '<div class="flow-node"><div class="ico" aria-hidden="true">💬</div>'
        "<h4>The AI you use</h4>"
        "<p>You paste one MCP address into a browser AI such as claude.ai</p></div>"
        '<div class="flow-arrow" aria-hidden="true">→</div>'
        '<div class="flow-node"><div class="ico" aria-hidden="true">🌳</div>'
        "<h4>NAMU Cloud</h4>"
        "<p>An errand runner that fetches your memory and writes it down</p></div>"
        '<div class="flow-arrow" aria-hidden="true">→</div>'
        '<div class="flow-node is-origin">'
        '<div class="ico" aria-hidden="true">🔒</div>'
        "<h4>Your GitHub repository</h4>"
        "<p><b>The original lives here</b>, as plain files</p></div>"
        "</div>"
        '<p class="flow-cap">Only a <b>working copy</b> ever passes through NAMU '
        "Cloud. Cut the connection and your memory stays right where it is, in "
        "your own repository.</p>"
    )


def _fork() -> str:
    """이 사이트가 맞는 사람인지 맨 앞에서 가른다(한국어판과 같은 이유).

    터미널 사용자를 이 길로 흘려보내면 반쪽짜리 나무를 쓰게 된다 — 이 주소로
    넘어가는 것은 기억과 파일뿐이고, 세션 브리핑·작업 절차·마무리 점검은
    플러그인에만 있다.
    """
    return (
        '<div class="fork">'
        '<div class="opt here"><span class="tag">You are in the right place</span>'
        "<h4>I use AI in a browser</h4>"
        "<p>Paste one MCP address into a browser AI such as claude.ai or ChatGPT "
        "and your memory carries over from that moment on. Going through NAMU "
        "Cloud gives you the memory and learning core; the workflow features are "
        "not included.</p>"
        '<a class="btn btn-primary" href="/en/start">See how to start</a></div>'
        '<div class="opt"><span class="tag">This is not for you</span>'
        "<h4>I use AI in a terminal</h4>"
        "<p>If you work in Claude Code or Antigravity CLI (agy), you should "
        "<b>install NAMU as a plugin</b> rather than paste an MCP address. The "
        "plugin gives you the workflow features alongside the memory and "
        "learning core.</p>"
        f'<a class="btn" href="{ui.INSTALL_GUIDE_URL}" target="_blank" '
        'rel="noopener">Plugin install guide (Korean) ↗</a></div>'
        "</div>"
    )


def _cta_row(logged_in: bool) -> str:
    if logged_in:
        return (
            '<div class="hero-cta">'
            '<a class="btn btn-primary btn-lg" href="/auth/me">Go to my page</a>'
            '<a class="btn btn-lg" href="/en/start">Read the walkthrough again</a>'
            "</div>"
        )
    return (
        '<div class="hero-cta">'
        '<a class="btn btn-primary btn-lg" href="/auth/github/login">'
        "Start with GitHub</a>"
        '<a class="btn btn-lg" href="/en/start">Look around first</a>'
        "</div>"
        '<p class="btn-note">A GitHub account is all you need · No card required</p>'
    )


# 로그인 뒤 화면이 한국어뿐이라는 고지. 세 화면에서 같은 문장을 쓰므로 한 곳에
# 둔다 — 서로 다른 말로 적히면 어느 쪽이 맞는지 방문자가 알 수 없다.
_KOREAN_AHEAD = (
    "<b>Heads-up:</b> the pages after sign-in are still Korean only. The "
    "connection address you receive works exactly the same, but the screens "
    "around it are not translated yet."
)


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------
def home_page(logged_in: bool = False) -> str:
    hero = (
        '<div class="hero"><div class="hero-in">'
        "<div>"
        '<span class="eyebrow">🌳 Memory for the AI you use in a browser</span>'
        "<h1>AI memory that<br>outlives the conversation</h1>"
        '<p class="lead">Stop explaining the same thing tomorrow that you '
        "explained today. Your memory accumulates as files in your own GitHub "
        "repository, and any AI you connect draws on that same repository.</p>"
        + _cta_row(logged_in)
        + "</div>"
        f'<div class="hero-art" aria-hidden="true">{pages.HERO_ART}</div>'
        "</div></div>"
    )

    where = ui.section(
        _flow_diagram(),
        eyebrow="First things first",
        title="Your memory piles up in your own place",
        sub="NAMU neither owns your memory nor stores it permanently. It only "
        "relays the memory files in <b>one repository</b> that you choose.",
        band=True,
    )

    fork = ui.section(
        _fork(),
        eyebrow="Before you begin",
        title="Where do you use AI?",
        sub="Installing NAMU works differently depending on whether you use AI "
        "in a browser or in a terminal. This site covers the browser case.",
    )

    how = ui.section(
        ui.steps(
            [
                (
                    "Sign in with GitHub",
                    "<p>This only confirms who you are. NAMU does not look "
                    "inside any repository at this step.</p>",
                ),
                (
                    "Prepare a repository for your memory",
                    "<p>One private repository is enough. If you do not have "
                    "one, we send you to a page with the name already filled "
                    "in, so it takes a single button.</p>",
                ),
                (
                    "Grant access to that repository only",
                    "<p>NAMU can see exactly one repository, the one you pick. "
                    "The others are never even listed to us.</p>",
                ),
                (
                    "Paste the address into your AI",
                    "<p>Copy one line and paste it into your AI's connector "
                    "settings. From then on you can pull memory out and put it "
                    "back mid-conversation.</p>",
                ),
            ]
        )
        + '<p style="margin-top:22px"><a class="btn" href="/en/start">'
        "Walk through it screen by screen →</a></p>",
        eyebrow="Four steps",
        title="You only go through this once",
        sub="Five minutes is plenty. If you close the window partway, you can "
        "pick it up again from <b>My page</b>.",
        band=True,
    )

    gains = ui.section(
        '<div class="grid grid-3">'
        '<div class="card"><h3>🔒 One private repository</h3>'
        "<p class='muted'>It is created inside your own GitHub. Every memory "
        "lands there as a file you can open and read yourself.</p></div>"
        '<div class="card"><h3>🔑 A connection address of your own</h3>'
        "<p class='muted'>One line to paste into your AI. You can reissue it or "
        "destroy it whenever you like.</p></div>"
        '<div class="card"><h3>📖 A screen for reading your memory</h3>'
        "<p class='muted'>See with your own eyes what the AI has remembered, "
        "and search through it.</p></div>"
        "</div>",
        eyebrow="What you end up with",
        title="Signing up leaves you these three things",
    )

    trust = ui.section(
        '<div class="grid grid-2">'
        '<div class="claim"><span class="ic" aria-hidden="true">🔒</span>'
        "<p><b>The original is yours</b>Memory accumulates in your repository; "
        "the server reads and writes a temporary copy of it.</p></div>"
        '<div class="claim"><span class="ic" aria-hidden="true">🎯</span>'
        "<p><b>One repository, nothing else</b>Our reach stops at the single "
        "repository you selected.</p></div>"
        '<div class="claim"><span class="ic" aria-hidden="true">⏱️</span>'
        "<p><b>We hold no lasting key</b>Access to GitHub uses short-lived "
        "tokens (one hour) issued as needed.</p></div>"
        '<div class="claim"><span class="ic" aria-hidden="true">🚪</span>'
        "<p><b>You can cut it off anytime</b>Destroy the address and no AI can "
        "reach your memory from that instant.</p></div>"
        "</div>"
        '<p style="margin-top:20px"><a class="btn" href="/en/safety">'
        "More about safety →</a></p>",
        eyebrow="Why you can rest easy",
        title="You are lending it, not handing it over",
        band=True,
    )

    # 한국어 홈의 `numbers`와 같은 자리다 — 숫자는 언어와 무관하게 같은 두
    # 자리에서 오고, 이름표만 영어로 바뀐다(`ui.live_stats`).
    numbers = ui.section(
        ui.live_stats("en"),
        eyebrow="So far",
        title="How many people have come by",
        band=True,
    )

    closing = ui.section(
        '<div class="card card-accent" style="text-align:center">'
        "<h3 style='margin-top:6px'>Ready to start?</h3>"
        "<p class='muted'>With a GitHub account it takes five minutes.</p>"
        + (
            '<a class="btn btn-primary btn-lg" href="/auth/me">Go to my page</a>'
            if logged_in
            else '<a class="btn btn-primary btn-lg" href="/auth/github/login">'
            "Start with GitHub</a>"
        )
        + "</div>"
    )

    return ui.page(
        "NAMU Cloud — AI memory that outlives the conversation",
        hero + where + fork + how + gains + trust + numbers + closing,
        current="/en",
        cta="me" if logged_in else "start",
        description="A service that gives browser-based AI a memory. The "
        "original of that memory accumulates as files in your own GitHub "
        "repository.",
        reveal=True,
        raw_body=True,
        ask=False,
        lang="en",
    )


# ---------------------------------------------------------------------------
# Get started
# ---------------------------------------------------------------------------
def start_page(logged_in: bool = False) -> str:
    body = (
        '<span class="eyebrow">Get started</span>'
        "<h1>From signing up to your first memory</h1>"
        '<p class="lead">Just follow what the screens tell you. What is written '
        "here is exactly what you will see.</p>"
        + ui.notice(_KOREAN_AHEAD, tone="info")
        + ui.notice(
            "This walkthrough is for creating and registering an MCP address so "
            "you can use NAMU with a browser AI. If you work in Claude Code or "
            "Antigravity CLI (agy) and want NAMU there, the "
            f'<a href="{ui.INSTALL_GUIDE_URL}" target="_blank" '
            'rel="noopener">plugin install guide (Korean)</a> is what you want '
            "instead.",
            tone="warn",
        )
        + "<h2>What you need</h2>"
        "<ul><li><b>A GitHub account</b> — make one first if you do not have "
        "it. It is free.</li>"
        "<li><b>A repository to hold your memory</b> — optional for now; step 2 "
        "below creates it.</li></ul>"
        + ui.steps(
            [
                (
                    "Sign in with GitHub",
                    '<p><a class="btn btn-primary" href="/auth/github/login">'
                    "Start with GitHub</a></p>"
                    "<p>GitHub shows you an <b>identity</b> screen. It says "
                    "nothing about repository access, and that is correct — "
                    "access is asked for separately in step 3.</p>",
                ),
                (
                    "Set up a repository for your memory",
                    "<p>Think of a repository as one folder inside your GitHub. "
                    "Every memory NAMU writes lands inside it as a file.</p>"
                    f'<p><a class="btn" href="{pages.NEW_REPO_URL}" '
                    'target="_blank" rel="noopener">Create one on GitHub ↗'
                    "</a></p>"
                    "<p>It opens with the name (<code>namu-memory</code>) and "
                    "<b>Private</b> already filled in — press the create button "
                    "once and come back. An existing repository works too.</p>",
                ),
                (
                    "Grant access to that repository only",
                    "<p>This time a <b>different screen</b> appears, asking "
                    "about repository access. Pick only the repository you just "
                    "made.</p>"
                    "<p>If you select several, the next screen asks you to "
                    "settle on one to hold your memory.</p>",
                ),
                (
                    "Paste the address you receive into your AI",
                    "<p>Once the connection is done, an address of this shape "
                    "appears on screen.</p>"
                    f"<pre><code>{_URL_SHAPE}</code></pre>"
                    "<p>In Claude, go to <b>Settings → Connectors → Add custom "
                    "connector</b>, paste it in and save. The name can be "
                    "anything you like (NAMU, for instance).</p>"
                    "<p>For a different AI, change <code>client=claude</code> at "
                    "the end of the address to that AI's name (for example "
                    "<code>client=chatgpt</code>). It is the label you later use "
                    "to filter <i>which AI wrote this</i>, so once you choose a "
                    "value, keep using the same one.</p>",
                ),
            ]
        )
        + "<h2>Once it is connected</h2>"
        "<p>Mid-conversation your AI can now <b>recall memory</b>, <b>record "
        "memory</b> and <b>search memory</b>. <b>Uploading and downloading "
        "files</b> comes along with it. To begin, just say something like "
        '"pull up what you remember about me."</p>'
        '<div class="card card-soft">'
        "<h4>If you lose the address</h4>"
        "<p class='muted' style='margin:0'>Closing the window is fine. It is "
        'always waiting on <a href="/auth/me">My page</a>.</p>'
        "</div>"
        + ui.notice(
            "<b>This address is effectively a password.</b> Anyone who has it "
            "can read and write your memory, so keep it out of chats and "
            "screenshots. If it does leak, "
            '<a href="/en/safety">reissue the address</a> and the old one is '
            "blocked immediately.",
            tone="bad",
        )
    )
    return ui.page(
        "Get started — NAMU Cloud",
        body,
        current="/en/start",
        cta="me" if logged_in else "start",
        description="Four steps, from signing up for NAMU Cloud to pasting the "
        "connection address into a browser AI.",
        ask=False,
        lang="en",
    )


# ---------------------------------------------------------------------------
# What it remembers
# ---------------------------------------------------------------------------
def memory_page(logged_in: bool = False) -> str:
    body = (
        '<span class="eyebrow">What it remembers</span>'
        "<h1>Every memory is written in three layers</h1>"
        '<p class="lead">A one-line note leaves you unable to tell later why it '
        "mattered; a raw dump is unreadable in a list. So each entry carries "
        "all three layers together.</p>"
        '<div class="grid grid-3">'
        '<div class="card"><span class="pill">Layer 1</span><h3>What</h3>'
        "<p class='muted'>The one-line summary. It is the only layer that goes "
        "into lists, so it is written once when the memory is saved and then "
        "left alone.</p></div>"
        '<div class="card"><span class="pill">Layer 2</span><h3>Why</h3>'
        "<p class='muted'>How it came to be known and why it is worth keeping. "
        "Without this you end up having the same conversation again from "
        "scratch.</p></div>"
        '<div class="card"><span class="pill">Layer 3</span><h3>What happened</h3>'
        "<p class='muted'>The full account and the source text. There is no "
        "length limit, so an entire piece of research fits.</p></div>"
        "</div>"
        "<h2>There are five containers</h2>"
        "<p>Nothing is piled up at random; entries are sorted by what kind of "
        "thing they are. Mixed together, they become impossible to find.</p>"
        "<table><thead><tr><th>Container</th><th>What goes in</th>"
        "<th>Example</th></tr></thead><tbody>"
        "<tr><td><b>Learnings</b></td><td>Lessons worth reusing</td>"
        "<td>This approach failed, and here is why</td></tr>"
        "<tr><td><b>Profile</b></td><td>Facts about you</td>"
        "<td>I like tables and dislike long prose</td></tr>"
        "<tr><td><b>Tasks</b></td><td>A record of work in progress</td>"
        "<td>How far it got, and what comes next</td></tr>"
        "<tr><td><b>Memos</b></td><td>Notes meant to be thrown away</td>"
        "<td>The raw material I just gathered</td></tr>"
        "<tr><td><b>Attachments</b></td><td>The history of uploaded files</td>"
        "<td>Why this report was uploaded</td></tr>"
        "</tbody></table>"
        '<div class="card card-soft">'
        "<h4>Only memos can be deleted</h4>"
        "<p class='muted' style='margin:0'>Learnings and profile facts cannot be "
        "edited or deleted from the screen. Reaching back into accumulated "
        "learning erases <b>what was known at the time</b>. To fix something "
        "wrong, tell the AI and let it record a correction; the older entry "
        "then steps aside.</p>"
        "</div>"
        "<h2>It holds your files too</h2>"
        "<p>Upload something you wrote in a conversation, or a file already on "
        "your machine, and fetch just that one back later from any AI. Files go "
        "straight into <b>your repository</b>; NAMU keeps only the <b>record</b> "
        "of what was uploaded and why. Images, PDFs and other non-text or large "
        "files travel through a <b>one-time link</b>, which never passes through "
        "the AI at all and is therefore fast regardless of size.</p>"
        "<h2>Work logs can be written from this address too</h2>"
        "<p>One thing differs: <b>when writing one, you must name the "
        "project</b>. On a PC, NAMU treats the folder you have open as the "
        "project, but the web has no such notion. When you are only "
        "<b>reading</b>, you can leave it out and see everything together.</p>"
        "<h2>How it behaves today</h2>"
        "<ul>"
        "<li><b>Recall</b> sweeps several containers at once and returns what is "
        "recent.</li>"
        "<li><b>Search</b> covers <b>all five containers</b> by keyword. Only "
        "entries containing <b>every</b> space-separated word match, and the "
        "order does not matter.</li>"
        "<li>The same memory can be reached from any AI you connect. Who wrote "
        "what is told apart by the label at the end of the address.</li>"
        "</ul>"
        '<p style="margin-top:26px">'
        + (
            '<a class="btn btn-primary" href="/auth/memory">Open my memory</a>'
            if logged_in
            else '<a class="btn btn-primary" href="/auth/github/login">'
            "Start with GitHub</a>"
        )
        + "</p>"
    )
    return ui.page(
        "What it remembers — NAMU Cloud",
        body,
        current="/en/memory",
        cta="me" if logged_in else "start",
        description="The three layers of a NAMU memory (what, why, what "
        "happened), the five containers, and how files are exchanged.",
        ask=False,
        lang="en",
    )


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
def safety_page(logged_in: bool = False) -> str:
    body = (
        '<span class="eyebrow">Safety</span>'
        "<h1>Before you trust it with your memory</h1>"
        '<p class="lead">Memory is personal material. So this page states '
        "plainly what the service can and cannot do.</p>"
        "<h2>The original stays in your repository</h2>"
        "<p>NAMU Cloud keeps a <b>working copy</b> of your repository on the "
        "server, reads and writes memory there, and pushes it back to your "
        "repository. Even if you stop using the service, your memory remains as "
        "files in your own repository.</p>"
        "<h2>One repository, nothing else</h2>"
        "<p>NAMU can reach exactly <b>one repository</b>: the one you selected "
        "when granting access. Other repositories are never even listed to us. "
        "Access uses <b>one-hour tokens</b> issued on demand, and no lasting key "
        "is stored.</p>"
        '<div class="card card-soft">'
        "<h4>Why we do not create the repository for you</h4>"
        "<p class='muted' style='margin:0'>GitHub does not offer a "
        "'create a repository' permission on its own — <b>creating, deleting "
        "and changing settings</b> come as one bundle. That would put the line "
        "<b>\"this app can delete your repositories\"</b> on every member's "
        "consent screen. For a service you trust with your memory, that is not "
        "a fair price for saving one click. Instead we send you to a page with "
        "the name and privacy setting already filled in.</p>"
        "</div>"
        "<h2>The connection address is your identity</h2>"
        "<p>Authentication in this service is <b>a single personal key</b> "
        "carried in the address. On every request the server decides who you "
        "are from that key and cuts off anything it does not recognise. Put "
        "another way: <b>whoever knows the address is treated as you.</b></p>"
        + ui.notice(
            "Do not post the address in chats, issues or screenshots. If it "
            "leaks by accident, <b>reissue</b> it from My page and the old one "
            "is blocked that instant.",
            tone="warn",
        )
        # 한국어판과 같은 자리 — 말풍선의 한 줄로는 다 담지 못하는 내용을 여기
        # 편다. 영어 화면에는 말풍선이 없지만, 방문자가 한국어 화면으로 건너가
        # 그것을 쓸 수 있으므로 고지는 양쪽에 그대로 둔다.
        + '<h2 id="ai">Asking the on-screen guide sends your words outside</h2>'
        + "<p>The assistant bubble in the corner of the <b>Korean</b> pages "
        "sends what you type to <b>Google's AI</b>. NAMU is on the <b>free "
        "tier</b>, and under those terms Google <b>may use what is sent and "
        "what comes back to build its products, and people may read and "
        "annotate it.</b> So do not type personal information, connection "
        "addresses or keys into that bubble.</p>"
        "<p>What leaves is <b>only what the visitor typed.</b> The material the "
        "assistant consults is limited to this site's pages and the published "
        "guide; <b>it has no path to your memory at all</b> — your memory is not "
        "in what it reads, and it never calls the functions that read memory. "
        "Nothing exchanged is <b>stored anywhere.</b> Close the window and it is "
        "gone.</p>"
        + ui.notice(
            "The free tier was not chosen for the price alone. What we would not "
            "choose is using it without this notice — and if we later move to a "
            "paid tier that makes this paragraph unnecessary, we will rewrite "
            "what stands here.",
            tone="info",
        )
        + "<h2>The address is managed from My page</h2>"
        + ui.steps(
            [
                (
                    "Connection test",
                    "<p>The server checks on your behalf whether the address is "
                    "actually responding. The verdict is one of three: <b>alive, "
                    "address is wrong, or cannot tell right now</b>.</p>"
                    "<p class='muted'>That last verdict <b>does not mean the "
                    "address is broken</b> — it happens for passing reasons, "
                    "such as right after the server restarts. Try again in a "
                    "minute or two.</p>",
                ),
                (
                    "Reissue the address",
                    "<p>Creates a new address and blocks the old one at once. "
                    "You will need to update the connector in your AI to the new "
                    "address as well.</p>",
                ),
                (
                    "Destroy the address",
                    "<p>Removes the address entirely. From that moment no AI can "
                    "reach your memory. The memory itself stays in your "
                    "repository, so if you issue a new address later you carry "
                    "on where you left off.</p>",
                ),
            ]
        )
        + "<h2>To leave completely</h2>"
        "<ol>"
        "<li><b>Destroy the address</b> from My page.</li>"
        "<li><b>Revoke the NAMU app's permission</b> in your GitHub "
        "settings.</li>"
        "<li>The repository holding your memory is yours, so leave it or delete "
        "it as you prefer.</li>"
        "</ol>"
        '<p style="margin-top:26px">'
        + (
            '<a class="btn btn-primary" href="/auth/me">Open my page</a>'
            if logged_in
            else '<a class="btn btn-primary" href="/auth/github/login">'
            "Start with GitHub</a>"
        )
        + "</p>"
    )
    return ui.page(
        "Safety — NAMU Cloud",
        body,
        current="/en/safety",
        cta="me" if logged_in else "start",
        description="Where the original of your memory lives, what NAMU can "
        "reach, and how to manage the address or leave altogether.",
        ask=False,
        lang="en",
    )


# ---------------------------------------------------------------------------
# FAQ
# ---------------------------------------------------------------------------
def faq_page(logged_in: bool = False) -> str:
    body = (
        '<span class="eyebrow">FAQ</span>'
        "<h1>Things you might be wondering</h1>"
        '<p class="lead">If yours is not here, leave it on the GitHub '
        "repository.</p>"
        + ui.faq(
            [
                # 한국어판과 같은 이유로 '돈이 드나요?'가 아니라 이 질문이
                # 먼저다(2026-08-15 사용자 결정) — 값 이야기를 앞세우면 아직
                # 값을 매기지도 않은 서비스에 먼저 경계가 선다.
                (
                    "Is it free?",
                    "<p><b>Yes, it is free.</b> NAMU Cloud is at the stage of "
                    "being used by a small number of people whose feedback "
                    "shapes it. No discussion of charging for the service has "
                    "taken place, and it is offered free within what the server "
                    "can carry.</p>"
                    "<p>There is no payment screen and no field for card "
                    "details. Telling us what got in your way is the most "
                    "useful thing you can give NAMU right now.</p>",
                ),
                (
                    "What is a repository?",
                    "<p>Think of it as one folder inside your GitHub. Every "
                    "memory NAMU writes piles up in there as a text file. You "
                    "can open and read them yourself, or download the lot.</p>",
                ),
                (
                    "Can I make the repository private?",
                    "<p>Private is what we recommend. When we send you to the "
                    "creation page, private is already selected.</p>",
                ),
                (
                    "Can I use it from another computer?",
                    "<p>Yes. Your memory lives in your repository, and with the "
                    "address you pull the same memory from anywhere. Any device "
                    "with a browser works.</p>",
                ),
                (
                    "Can I connect several AIs at once?",
                    "<p>Yes. Just change the label at the end of the address to "
                    "that AI's name (for example <code>client=chatgpt</code>). "
                    "The memory gathers in one place, and the label tells you "
                    "who wrote what.</p>"
                    "<p><b>Once you choose a name, keep using the same one</b> — "
                    "<code>claude</code> and <code>cld</code> are stored as two "
                    "different things.</p>",
                ),
                (
                    "Does the AI read all of my memory?",
                    "<p>When the AI recalls memory mid-conversation, that "
                    "content enters the conversation. So it is better not to "
                    "record what you would not want surfaced. You can check "
                    "what has been kept at any time on the <b>My memory</b> "
                    "screen.</p>",
                ),
                (
                    "I use Claude Code or Antigravity CLI (agy).",
                    "<p>Then do not paste this address. Only memory and files "
                    "travel through it; the other half of NAMU — session "
                    "briefings, work procedures, closing checks — does not come "
                    "along. Your memory would also end up split across two "
                    "places.</p>"
                    f'<p><a class="btn" href="{ui.INSTALL_GUIDE_URL}" '
                    'target="_blank" rel="noopener">Plugin install guide '
                    "(Korean) ↗</a></p>",
                ),
                (
                    "I would rather run the server myself.",
                    "<p>That path exists. The difference is a single one: "
                    "whether you stand the server up and maintain it, or NAMU "
                    "does it for you.</p>"
                    f'<p><a class="btn" href="{ui.SELFHOST_GUIDE_URL}" '
                    'target="_blank" rel="noopener">Running your own server '
                    "(Korean) ↗</a></p>",
                ),
                (
                    "How do I leave?",
                    "<p>Destroy the address on My page, then revoke the NAMU "
                    "app's permission in your GitHub settings. The repository "
                    "holding your memory is yours and stays put.</p>"
                    '<p><a class="btn" href="/en/safety">More on the safety '
                    "page →</a></p>",
                ),
                (
                    "Are the pages after sign-in in English?",
                    "<p>Not yet. The public pages you are reading now are "
                    "available in English, but the screens after sign-in — "
                    "My page, My memory and the setup steps — are still Korean "
                    "only. The connection address itself works exactly the "
                    "same.</p>",
                ),
            ]
        )
        + '<div class="card card-accent" style="margin-top:30px">'
        "<h4>Not possible yet</h4>"
        "<p class='muted' style='margin:0'>Editing memory from the web is not "
        "supported yet (removing a memo is the one exception). The connection "
        "address is something a person collects in a browser and pastes by "
        "hand.</p>"
        "</div>"
    )
    return ui.page(
        "FAQ — NAMU Cloud",
        body,
        current="/en/faq",
        cta="me" if logged_in else "start",
        description="Cost, repositories, connecting several AIs, leaving the "
        "service, and other questions people ask about NAMU Cloud.",
        ask=False,
        lang="en",
    )


# 경로 → 그리는 함수. 한국어판(`pages.PAGES`)과 **따로 둔다** — `ask_corpus`가
# 안내원의 자료를 만들 때 `pages.PAGES`를 통째로 돌리므로, 여기 것을 그 사전에
# 섞으면 한국어 안내원이 영어 화면 글까지 근거로 물고 온다.
PAGES = {
    "/en": home_page,
    "/en/start": start_page,
    "/en/memory": memory_page,
    "/en/safety": safety_page,
    "/en/faq": faq_page,
}
