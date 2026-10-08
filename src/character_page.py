"""캐릭터 만들기 — 공개 화면 `/character` (나무 캐릭터 설계서 0·11장).

claude.ai에서 만든 프로토타입(character-maker.html)을 홈페이지로 옮긴 것이다.
질문을 하나씩 묻고, 답할 때마다 가지에 잎이 하나씩 돋으며, 끝나면 설계서 5.1
모양의 카드 JSON을 보여 준다. 로그인 없이 누구나 열 수 있다.

## 질문은 서버 정의에서 받는다

프로토타입은 질문 목록(`STEPS`)을 화면 안에 손으로 적어 두었다. 여기서는 그것을
버리고 코어의 `character.schema()`(질문·약속·카드 칸)와 천장 규칙 상수
(`CEILINGS`의 rank, `START_MIN_CEILING_RANK`)를 **화면을 그릴 때 JSON으로 실어**
보낸다. 대화로 만들기(`namu_character_schema` 도구)와 이 화면이 같은 틀 하나를
쓰게 하기 위해서다(설계서 5.1 — 붕어빵 틀은 하나). 천장 규칙의 숫자는 스키마의
`rules`(사람이 읽는 문장)에 없으므로 모듈 상수에서 직접 읽는다.

따로 읽기 주소(API)를 두지 않고 화면에 실어 보내는 이유: 질문은 서버를 다시
띄우기 전까지 바뀌지 않는 고정 값이라 한 번 더 왕복할 까닭이 없고, 주소를 하나
늘리면 문 목록(`ui.API_PATHS`)도 함께 늘어난다.

## 왜 pages.py가 아니라 따로 두나

`pages.PAGES`는 AI 안내원의 말뭉치(`ask_corpus.homepage_text`)가 통째로 읽는
사전이다. 이 화면의 본문은 대부분 자바스크립트가 그리고 화면에 실린 것은 질문
JSON이라, 거기 섞으면 안내원 자료에 의미 없는 글이 들어간다. 그래서 그리는
함수는 여기 두고, 라우트를 거는 쪽(`web_auth._ALL_PUBLIC_PAGES`)에서만 합친다.
문 목록은 `ui.UNLISTED`가 원본이다.

## 저장 — 로그인 전엔 초안만, 로그인 뒤엔 서버에

만들던 내용은 로그인 여부와 상관없이 늘 브라우저(localStorage)에 먼저 남는다.
로그인하지 않은 채로는 "저장하기" 대신 로그인 유도 단추만 보이고, 서버에는
아무것도 쓰이지 않는다 — 결과는 복사하기로 직접 가져갈 수 있다. 로그인한
뒤에는 "저장하기"가 `/auth/character/save`(POST, `web_auth.character_save`)로
카드를 보내 저장한다. 그 주소가 로그인·저장소 연결·비공개 확인까지 다시
검사하므로, 이 화면은 로그인 여부만 보고 단추를 가를 뿐 보안 판단을 하지
않는다. 공개 저장소로는 서버가 저장을 거절한다(기존 설계 원칙 — 캐릭터
일기는 비공개 저장소에만 쓴다).

## 이번에 하지 않는 것(설계서 0장의 다음 단계)

위쪽 메뉴의 "캐릭터", 영어판 `/en/character`, 로그인 뒤 내 캐릭터를 목록으로
보여주는 `/auth/character`.
"""
import html
import json
import sys
from pathlib import Path

import ui

PATH = "/character"

# 만들던 내용을 남기는 브라우저 저장 칸 이름. 프로토타입(`namu-character-draft-v3`)과
# 일부러 다르게 둔다 — 그쪽은 키 이름이 서버 스키마와 달라(callme·start·warmth,
# 화면 이름으로 저장) 그대로 읽으면 엉뚱한 값이 들어온다.
DRAFT_KEY = "namu-cloud-character-draft-v1"

_character_module = None


def _character():
    """코어의 `character` 모듈을 지연 로드한다(`web_auth._core`와 같은 방식).

    import 시점에 vendor 경로를 얹지 않는 이유도 같다 — 이 파일을 불러오는 것만으로
    `sys.path`가 바뀌면 이름이 겹치는 모듈이 어느 쪽으로 풀릴지가 import 순서에
    좌우된다. 실서버에서는 routing_server가 이미 얹어 두었으므로 여기서 하는 일이 없다.
    """
    global _character_module
    if _character_module is None:
        vendor_plugin = (
            Path(__file__).resolve().parent.parent / "vendor" / "namu-agent" / "namu-plugin"
        )
        if str(vendor_plugin) not in sys.path:
            sys.path.append(str(vendor_plugin))
        import character

        _character_module = character
    return _character_module


def schema_data() -> dict:
    """화면에 실어 보낼 캐릭터 틀. 전부 코어에서 읽고, 여기서 새로 정하는 값은 없다."""
    ch = _character()
    s = ch.schema()
    # 카드 칸의 순서는 스키마 예시(설계서 5.1과 같은 순서)를 따른다. 예시와 칸 목록이
    # 어긋나는 날에는 칸 목록이 원본이므로 그쪽을 쓴다.
    example_keys = list(s.get("example", {}))
    card_keys = example_keys if set(example_keys) == set(s["card_keys"]) else s["card_keys"]
    return {
        "schema_version": s["schema_version"],
        "questions": s["questions"],
        "promises": s["promises"],
        "card_keys": list(card_keys),
        "ceiling_rank": {c["value"]: c["rank"] for c in ch.CEILINGS},
        "start_min_ceiling_rank": dict(ch.START_MIN_CEILING_RANK),
    }


def _json_for_script(data: dict) -> str:
    """`<script type="application/json">` 안에 넣어도 안전한 JSON 글자.

    `<`를 그대로 두면 질문 문구에 `</script>`가 끼는 날 태그가 거기서 닫힌다.
    `&`·`>`도 같은 이유로 바꾼다. JSON.parse는 `\\u003c`를 `<`로 그대로 되돌린다.
    """
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


# 이 화면에만 쓰는 조각. 이름 앞에 `cm-`를 붙인다 — 공통 부품(ui.py)에 이미
# `.grid`·`.card`가 있어, 프로토타입의 이름을 그대로 쓰면 서로 덮어쓴다.
# 색은 전부 ui.py의 테마 토큰이라 밝게/어둡게가 다른 화면과 함께 바뀐다.
_CSS = (
    "<style>"
    ".cm-head{margin:0 0 24px;max-width:60ch;}"
    ".cm-head h1{margin-bottom:.3em;}"
    ".cm-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);"
    "gap:24px;align-items:start;}"
    "@media (max-width:820px){.cm-grid{grid-template-columns:minmax(0,1fr);}}"
    ".cm-panel,.cm-card{background:var(--bg-card);border:1px solid var(--border);"
    "border-radius:var(--radius);box-shadow:var(--shadow);}"
    ".cm-panel{padding:26px;}"
    ".cm-stepnum{color:var(--fg-faint);font-size:.88rem;font-weight:700;}"
    ".cm-panel h2{font-size:1.35rem;margin:4px 0 6px;}"
    ".cm-hint{color:var(--fg-soft);margin:0 0 18px;}"
    ".cm-chips{display:flex;flex-wrap:wrap;gap:9px;}"
    ".cm-chip{font:inherit;font-size:.95rem;color:var(--fg);background:transparent;"
    "cursor:pointer;border:1.5px solid var(--border-strong);border-radius:999px;"
    "padding:7px 15px;}"
    ".cm-chip:hover{border-color:var(--accent);}"
    '.cm-chip[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);'
    "color:var(--on-accent);}"
    ".cm-chip.custom{border-style:dashed;}"
    ".cm-opts{display:grid;gap:10px;}"
    ".cm-opt{font:inherit;color:var(--fg);text-align:left;cursor:pointer;"
    "background:transparent;border:1.5px solid var(--border-strong);"
    "border-radius:12px;padding:13px 16px;}"
    ".cm-opt:hover{border-color:var(--accent);}"
    ".cm-opt strong{display:block;}"
    ".cm-opt span{color:var(--fg-soft);font-size:.92rem;}"
    '.cm-opt[aria-pressed="true"]{border-color:var(--accent);background:var(--accent-soft);}'
    ".cm-opt:disabled{opacity:.4;cursor:not-allowed;}"
    ".cm-custom{display:flex;gap:8px;margin-top:16px;}"
    ".cm-custom input{flex:1;min-width:0;font-size:15px;}"
    ".cm-count{color:var(--fg-faint);font-size:.85rem;margin:8px 0 0;}"
    ".cm-nav{display:flex;justify-content:space-between;gap:12px;margin-top:26px;}"
    ".cm-panel .btn:disabled{opacity:.4;cursor:not-allowed;box-shadow:none;}"
    ".cm-card{padding:0 22px 22px;position:sticky;top:74px;}"
    "@media (max-width:820px){.cm-card{position:static;}}"
    ".cm-twig{display:block;width:100%;height:84px;margin-bottom:4px;}"
    # 잎은 한 단계를 마치고 넘어갈 때 눈(작은 점)에서 옅은 초록으로 돋아 짙은 초록으로
    # 짙어진다 — "잎이 돋는다"는 뜻이 바로 읽히도록 사이트 강조색과는 다른 색을 쓴다.
    ".cm-twig{--cm-leaf:#4f8a4b;--cm-leaf-light:#8cc47f;}"
    "@media (prefers-color-scheme:dark){:root:not([data-theme=\"light\"]) .cm-twig{"
    "--cm-leaf:#7cbf72;--cm-leaf-light:#b5e3a8;}}"
    ":root[data-theme=\"dark\"] .cm-twig{--cm-leaf:#7cbf72;--cm-leaf-light:#b5e3a8;}"
    ".cm-twig .bud{fill:var(--border-strong);}"
    ".cm-twig .leafshape{fill:var(--cm-leaf-light);opacity:0;transform:scale(0);"
    "transform-box:fill-box;}"
    ".cm-twig .leafshape.on{opacity:1;transform:scale(1);fill:var(--cm-leaf);"
    "transition:transform .8s cubic-bezier(.3,1.6,.5,1),opacity .3s ease,fill 1.4s ease .3s;}"
    ".cm-twig .spark{fill:none;stroke:var(--cm-leaf-light);stroke-width:1.5;opacity:0;}"
    ".cm-twig .spark.go{animation:cm-spark .9s ease-out;}"
    "@keyframes cm-spark{0%{opacity:.9;r:2px;}100%{opacity:0;r:16px;}}"
    ".cm-twig .stem{stroke:var(--fg-faint);stroke-width:2;fill:none;stroke-linecap:round;}"
    ".cm-card h3{font-size:1.45rem;line-height:1.25;margin:0;}"
    ".cm-sub{color:var(--fg-soft);margin:2px 0 14px;font-size:.93rem;}"
    ".cm-card dl{margin:0;display:grid;gap:9px;}"
    ".cm-card dt{color:var(--fg-faint);font-size:.83rem;font-weight:600;}"
    ".cm-card dd{margin:0;word-break:break-word;}"
    ".cm-card dd.empty{color:var(--fg-faint);}"
    ".cm-promise{margin-top:18px;padding-top:14px;border-top:1px dashed var(--border-strong);"
    "font-size:.9rem;color:var(--fg-soft);}"
    ".cm-promise ul{margin:6px 0 0;padding-left:18px;}"
    ".cm-result{display:none;margin-top:26px;}"
    ".cm-result.show{display:block;}"
    ".cm-result h2{font-size:1.2rem;margin:0 0 10px;}"
    ".cm-tabs{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;}"
    ".cm-tabs .btn{margin-left:auto;}"
    ".cm-out{white-space:pre-wrap;word-break:break-word;font-size:.88rem;line-height:1.6;"
    "max-height:420px;overflow:auto;margin:0;}"
    ".cm-toast{color:var(--ok);font-size:.9rem;}"
    ".cm-save{display:flex;align-items:center;gap:12px;flex-wrap:wrap;"
    "margin:16px 0 22px;padding-top:16px;border-top:1px dashed var(--border-strong);}"
    ".cm-save-msg{font-size:.9rem;color:var(--fg-soft);}"
    ".cm-save-msg.ok{color:var(--ok);}"
    ".cm-save-msg.err{color:var(--danger);}"
    "@media (max-width:520px){.cm-panel{padding:20px 18px;}.cm-card{padding:0 18px 18px;}}"
    "@media (prefers-reduced-motion:reduce){.cm-twig .leafshape.on{transition:none;}"
    ".cm-twig .spark.go{animation:none;}}"
    "</style>"
)

# 화면을 움직이는 스크립트. 질문 목록은 위의 JSON(`#cm-schema`)에서만 읽는다.
# 잎 그림(`renderLeaves`)의 위치 계산과 잎 모양은 프로토타입 그대로다.
_SCRIPT = r"""<script>
(function(){
const S = JSON.parse(document.getElementById('cm-schema').textContent);
const QS = S.questions;
const KEY = __DRAFT_KEY__;
const LOGGED_IN = __LOGGED_IN__;
const SAVED_KEY = KEY + ':saved';
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const byKey = {}; QS.forEach(q => { byKey[q.key] = q; });

/* 브라우저에 남은 초안을 읽되, 지금 틀에 맞지 않는 값은 버린다
   (질문이 바뀌었거나 손으로 고친 값이 섞여 있어도 화면이 깨지지 않게). */
function clean(raw){
  const out = {};
  if (!raw || typeof raw !== 'object') return out;
  QS.forEach(q => {
    const v = raw[q.key];
    const cut = s => typeof s === 'string' ? (q.max_length ? s.slice(0, q.max_length) : s) : '';
    if (q.type === 'multi') {
      if (!Array.isArray(v)) return;
      const arr = [];
      v.forEach(x => { const t = cut(x).trim(); if (t && !arr.includes(t)) arr.push(t); });
      out[q.key] = arr.slice(0, q.max_items || arr.length);
    } else if (q.type === 'choice') {
      if (q.options.some(o => o.value === v)) out[q.key] = v;
    } else {
      const t = cut(v).trim(); if (t) out[q.key] = t;
    }
  });
  return out;
}
let state = {};
try { state = clean(JSON.parse(localStorage.getItem(KEY) || 'null')); } catch(e) { state = {}; }
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch(e) {} };
let step = 0;

const answered = q => { const v = state[q.key]; return Array.isArray(v) ? v.length > 0 : !!v; };
const optLabel = (q, v) => { const o = q.options.find(o => o.value === v); return o ? o.label : ''; };

/* 시작점보다 낮은 천장은 고를 수 없다(숫자는 서버 상수에서 온다). */
function ceilAllowed(value){
  const start = state.relationship_start;
  if (!start || !(start in S.start_min_ceiling_rank)) return true;
  return S.ceiling_rank[value] >= S.start_min_ceiling_rank[start];
}

/* 잎은 그 단계를 마치고 넘어갔을 때 돋는다. 다시 연 초안은 답이 있는 단계를 마친 것으로 본다. */
const PASSED_KEY = KEY + ':passed';
const passed = new Set();
QS.forEach((q, i) => { if (answered(q)) passed.add(i); });
try {
  const raw = JSON.parse(localStorage.getItem(PASSED_KEY) || '[]');
  if (Array.isArray(raw)) raw.forEach(i => { if (Number.isInteger(i) && i >= 0 && i < QS.length) passed.add(i); });
} catch(e) {}
const savePassed = () => { try { localStorage.setItem(PASSED_KEY, JSON.stringify([...passed])); } catch(e) {} };
const leafOn = i => passed.has(i) && (answered(QS[i]) || !QS[i].required);

/* 잎은 한 번만 그리고 이후에는 켜고 끄기만 한다 — 다시 그리면 돋는 움직임이 보이지 않는다. */
const NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const leaves = [], sparks = [];
function buildLeaves(){
  const g = $('cm-leaves');
  QS.forEach((q, i) => {
    const t = (i + 0.7) / QS.length;
    const x = 8 + t * 300;
    const y = 52 - t * 14 + Math.sin(t * Math.PI) * 4;
    const up = i % 2 === 0;
    const ty = up ? y - 2 : y + 2;
    g.appendChild(svgEl('circle', {class:'bud', cx:x, cy:ty, r:1.8}));
    const wrap = svgEl('g', {transform:`rotate(${up ? -28 : 28} ${x} ${ty})`});
    const p = svgEl('path', {d: up
      ? `M${x} ${ty} C ${x-9} ${ty-10}, ${x-4} ${ty-24}, ${x} ${ty-28} C ${x+4} ${ty-24}, ${x+9} ${ty-10}, ${x} ${ty} Z`
      : `M${x} ${ty} C ${x-9} ${ty+10}, ${x-4} ${ty+22}, ${x} ${ty+26} C ${x+4} ${ty+22}, ${x+9} ${ty+10}, ${x} ${ty} Z`});
    // 잎 밑동(가지에 붙은 쪽)에서 자라나게 한다.
    p.style.transformOrigin = up ? '50% 100%' : '50% 0%';
    p.setAttribute('class', 'leafshape' + (leafOn(i) ? ' on' : ''));
    wrap.appendChild(p); g.appendChild(wrap); leaves.push(p);
    const s = svgEl('circle', {class:'spark', cx:x, cy:up ? ty - 14 : ty + 13, r:2});
    g.appendChild(s); sparks.push(s);
  });
}
function renderLeaves(){
  leaves.forEach((p, i) => {
    const on = leafOn(i);
    if (on && !p.classList.contains('on')) {
      p.classList.add('on');
      sparks[i].classList.remove('go'); void sparks[i].getBBox(); sparks[i].classList.add('go');
    } else if (!on) p.classList.remove('on');
  });
}

function shown(q){
  const v = state[q.key];
  if (q.type === 'choice') return v ? optLabel(q, v) : '';
  if (Array.isArray(v)) return v.join(q.key === 'sample_lines' ? ' / ' : ', ');
  return v || '';
}

function renderCard(){
  const n = QS.filter((q, i) => leafOn(i)).length;
  $('cm-name').textContent = state.name || '이름 없음';
  $('cm-sub').textContent = n === 0 ? '아직 씨앗 상태예요' : n === QS.length ? '잎이 모두 돋았어요' : `잎 ${n}개가 돋았어요`;
  $('cm-list').innerHTML = QS.filter(q => q.key !== 'name').map(q => {
    const txt = shown(q);
    return `<div><dt>${esc(q.label)}</dt><dd class="${txt ? '' : 'empty'}">${txt ? esc(txt) : (q.required ? '아직 정하지 않았어요' : '없음')}</dd></div>`;
  }).join('');
  renderLeaves();
}

function renderStep(){
  $('cm-result').classList.remove('show');
  const q = QS[step];
  if (q.key === 'relationship_ceiling' && state.relationship_ceiling && !ceilAllowed(state.relationship_ceiling)) {
    delete state.relationship_ceiling; save();
  }
  const v = state[q.key];
  let body = '';
  if (q.type === 'choice') {
    body = `<div class="cm-opts">${q.options.map(o => {
      const dis = q.key === 'relationship_ceiling' && !ceilAllowed(o.value);
      return `<button type="button" class="cm-opt" data-v="${esc(o.value)}" aria-pressed="${v === o.value}" ${dis ? 'disabled title="관계 시작점보다 낮게 정할 수 없어요"' : ''}><strong>${esc(o.label)}</strong><span>${esc(o.desc || '')}</span></button>`;
    }).join('')}</div>`;
  } else {
    const multi = q.type === 'multi';
    const sel = Array.isArray(v) ? v : (v ? [v] : []);
    const customs = sel.filter(x => !q.options.includes(x));
    const full = multi && q.max_items && sel.length >= q.max_items;
    body = `<div class="cm-chips">${q.options.map(o =>
      `<button type="button" class="cm-chip" data-v="${esc(o)}" aria-pressed="${sel.includes(o)}">${esc(o)}</button>`).join('')}
      ${customs.map(c => `<button type="button" class="cm-chip custom" data-v="${esc(c)}" aria-pressed="true" title="누르면 빠져요">${esc(c)}</button>`).join('')}</div>
      <div class="cm-custom"><input type="text" id="cm-in" placeholder="${esc(q.custom_label || '')}" aria-label="${esc(q.custom_label || q.label)}"${q.max_length ? ` maxlength="${q.max_length}"` : ''}>
      <button type="button" class="btn" id="cm-add">${multi ? '추가' : '이걸로'}</button></div>
      ${multi && q.max_items ? `<p class="cm-count">${sel.length} / ${q.max_items}${full ? ' — 더 고르면 가장 먼저 고른 것이 빠져요' : ''}</p>` : ''}`;
  }
  $('cm-q').innerHTML = `<div class="cm-stepnum">${step + 1} / ${QS.length}${q.required ? '' : ' · 건너뛰어도 돼요'}</div>
    <h2>${esc(q.title)}</h2><p class="cm-hint">${esc(q.hint || '')}</p>${body}`;

  $('cm-q').querySelectorAll('[data-v]').forEach(b => b.addEventListener('click', () => pick(b.dataset.v)));
  const add = $('cm-add');
  if (add) {
    const go = () => { const t = $('cm-in').value.trim(); if (t) pick(q.max_length ? t.slice(0, q.max_length) : t, true); };
    add.addEventListener('click', go);
    $('cm-in').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
  }
  $('cm-prev').disabled = step === 0;
  const last = step === QS.length - 1;
  $('cm-next').textContent = last ? '카드 완성하기' : (!q.required && !answered(q) ? '건너뛰기' : '다음');
  $('cm-next').disabled = q.required && !answered(q);
  renderCard();
}

function pick(val, isCustom){
  const q = QS[step];
  if (q.type === 'multi') {
    let arr = Array.isArray(state[q.key]) ? [...state[q.key]] : [];
    if (arr.includes(val)) { if (!isCustom) arr = arr.filter(x => x !== val); }
    else { if (q.max_items && arr.length >= q.max_items) arr.shift(); arr.push(val); }
    if (arr.length) state[q.key] = arr; else delete state[q.key];
  } else {
    if (state[q.key] === val && !isCustom) delete state[q.key]; else state[q.key] = val;
  }
  save();
  renderStep();
}

/* 결과 카드 — 설계서 5.1 모양. 칸 순서와 칸 이름은 서버가 보낸 card_keys를 따른다. */
function buildCardJson(){
  const card = {};
  S.card_keys.forEach(k => {
    const q = byKey[k];
    if (k === 'schema_version') card[k] = S.schema_version;
    else if (k === 'id') card[k] = null;               // 서버가 저장할 때 붙인다
    else if (k === 'expression_level') card[k] = null; // 이 서버는 수위를 정하지 않는다
    else if (k === 'promises') card[k] = S.promises.slice();
    else if (!q) card[k] = null;
    else if (q.type === 'multi') card[k] = Array.isArray(state[k]) ? state[k].slice() : [];
    else if (q.type === 'choice') card[k] = state[k] || null;
    else card[k] = state[k] || '';
  });
  return card;
}
function buildPreview(){
  const c = buildCardJson();
  const L = a => Array.isArray(a) ? a.join(', ') : (a || '');
  const lines = [`# ${c.name}${c.aliases && c.aliases.length ? ` (별명: ${L(c.aliases)})` : ''}`, ''];
  QS.forEach(q => {
    if (q.key === 'name' || q.key === 'aliases' || q.key === 'sample_lines') return;
    lines.push(`${q.label}: ${shown(q)}`);
  });
  if (c.sample_lines && c.sample_lines.length) {
    lines.push('', '예시 대사');
    c.sample_lines.forEach(s => lines.push(`- "${s}"`));
  }
  lines.push('', '언제나 지키는 약속');
  c.promises.forEach(p => lines.push('- ' + p));
  lines.push('', '(실제 AI에게 건네는 설정 글은 나무 서버가 이 카드로 만들어요.)');
  return lines.join('\n');
}

/* 저장한 뒤 서버가 돌려준 id·version을 기억해 둔다 — 같은 카드를 다시
   저장할 때 새 캐릭터로 오해되지 않고(이름 중복 거절) 고치는 것으로
   이어지게 하기 위해서다. */
function getSaved(){
  try { return JSON.parse(localStorage.getItem(SAVED_KEY) || 'null'); }
  catch(e) { return null; }
}
function setSaved(v){ try { localStorage.setItem(SAVED_KEY, JSON.stringify(v)); } catch(e) {} }
function buildSaveCard(){
  const card = buildCardJson();
  const saved = getSaved();
  if (saved && saved.id) card.id = saved.id;
  return card;
}

if (LOGGED_IN) {
  $('cm-save').hidden = false;
  $('cm-save').addEventListener('click', async () => {
    const card = buildSaveCard();
    const saved = getSaved();
    const btn = $('cm-save'), msg = $('cm-save-msg');
    btn.disabled = true;
    msg.className = 'cm-save-msg';
    msg.textContent = '저장하는 중...';
    try {
      const res = await fetch('/auth/character/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
        body: JSON.stringify({card, base_version: saved ? saved.version : null}),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        setSaved({id: data.id, version: data.version});
        msg.className = 'cm-save-msg ok';
        msg.textContent = `저장했어요(${data.name}). 나무에 연결된 AI에게 이름을 말하면 불러와요.`;
      } else {
        msg.className = 'cm-save-msg err';
        msg.textContent = (data && data.message) || '저장하지 못했어요. 잠시 후 다시 시도해 주세요.';
      }
    } catch(e) {
      msg.className = 'cm-save-msg err';
      msg.textContent = '저장하지 못했어요 — 연결을 확인하고 다시 시도해 주세요.';
    } finally {
      btn.disabled = false;
    }
  });
} else {
  $('cm-save-login').hidden = false;
}

let mode = 'json';
function showResult(){
  $('cm-out').textContent = mode === 'json' ? JSON.stringify(buildCardJson(), null, 2) : buildPreview();
  $('cm-tab-json').setAttribute('aria-pressed', mode === 'json');
  $('cm-tab-md').setAttribute('aria-pressed', mode === 'md');
  $('cm-result').classList.add('show');
  $('cm-result').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block:'start'});
}

$('cm-prev').addEventListener('click', () => { if (step > 0) { step--; renderStep(); } });
$('cm-next').addEventListener('click', () => {
  passed.add(step); savePassed();
  if (step < QS.length - 1) { step++; renderStep(); } else { renderCard(); showResult(); }
});
$('cm-tab-json').addEventListener('click', () => { mode = 'json'; showResult(); });
$('cm-tab-md').addEventListener('click', () => { mode = 'md'; showResult(); });
$('cm-copy').addEventListener('click', async () => {
  const text = $('cm-out').textContent;
  try { await navigator.clipboard.writeText(text); $('cm-toast').textContent = '복사했어요'; }
  catch(e) {
    const r = document.createRange(); r.selectNodeContents($('cm-out'));
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    $('cm-toast').textContent = '선택해 뒀어요. Ctrl+C로 복사하세요';
  }
  setTimeout(() => { $('cm-toast').textContent = ''; }, 2500);
});

$('cm-nav').hidden = false;
buildLeaves();
renderStep();
})();
</script>"""


def character_page(logged_in: bool = False) -> str:
    data = schema_data()
    promises = "".join(f"<li>{html.escape(p)}</li>" for p in data["promises"])
    body = (
        _CSS
        + '<header class="cm-head">'
        '<span class="eyebrow">캐릭터 만들기</span>'
        "<h1>나무 에이전트의 첫 잎</h1>"
        '<p class="lead">질문에 답할 때마다 캐릭터가 한 잎씩 자라요. '
        "보기에서 고르거나 직접 써넣으면 돼요.</p>"
        "</header>"
        '<div class="cm-grid">'
        '<section class="cm-panel" aria-live="polite">'
        '<div id="cm-q"><noscript><p>이 화면은 자바스크립트가 켜져 있어야 '
        "움직여요.</p></noscript></div>"
        '<div class="cm-nav" id="cm-nav" hidden>'
        '<button type="button" class="btn" id="cm-prev">이전</button>'
        '<button type="button" class="btn btn-primary" id="cm-next">다음</button>'
        "</div>"
        '<div class="cm-result" id="cm-result">'
        "<h2>캐릭터 카드가 완성됐어요</h2>"
        '<p class="cm-hint">저장하면 내 저장소에 캐릭터로 남아요. JSON을 복사해 나무에 '
        '연결된 AI에게 "이 캐릭터 등록해줘"라고 붙여 넣어도 돼요.</p>'
        '<div class="cm-save">'
        '<button type="button" class="btn btn-primary" id="cm-save" hidden>저장하기</button>'
        '<a class="btn btn-primary" id="cm-save-login" href="/auth/github/login" hidden>'
        "로그인하고 저장하기</a>"
        '<span class="cm-save-msg" id="cm-save-msg" role="status"></span>'
        "</div>"
        '<div class="cm-tabs">'
        '<button type="button" class="cm-chip" id="cm-tab-json" aria-pressed="true">JSON</button>'
        '<button type="button" class="cm-chip" id="cm-tab-md" aria-pressed="false">미리보기 글</button>'
        '<button type="button" class="btn" id="cm-copy">복사하기</button>'
        "</div>"
        '<pre class="cm-out" id="cm-out"></pre>'
        '<span class="cm-toast" id="cm-toast" role="status"></span>'
        "</div>"
        "</section>"
        '<aside class="cm-card" aria-label="캐릭터 미리보기">'
        '<svg class="cm-twig" viewBox="0 0 320 84" aria-hidden="true">'
        '<path class="stem" d="M8 52 C 80 40, 160 60, 312 38"/>'
        '<g id="cm-leaves"></g>'
        "</svg>"
        '<h3 id="cm-name">이름 없음</h3>'
        '<p class="cm-sub" id="cm-sub">아직 씨앗 상태예요</p>'
        '<dl id="cm-list"></dl>'
        '<div class="cm-promise">언제나 지키는 약속'
        f"<ul>{promises}</ul></div>"
        "</aside>"
        "</div>"
        f'<script type="application/json" id="cm-schema">{_json_for_script(data)}</script>'
        + _SCRIPT.replace("__DRAFT_KEY__", json.dumps(DRAFT_KEY))
        .replace("__LOGGED_IN__", "true" if logged_in else "false")
    )
    return ui.page(
        "캐릭터 만들기 — 나무 클라우드",
        f'<div class="wrap-wide" style="padding-top:36px">{body}</div>',
        current=PATH,
        cta="me" if logged_in else "start",
        description="질문에 하나씩 답하며 나무 에이전트의 캐릭터 카드를 만들어 보세요. "
        "로그인 없이 만들어 볼 수 있어요.",
        raw_body=True,
    )


# 경로 → 그리는 함수. `web_auth._ALL_PUBLIC_PAGES`가 이 사전을 합쳐 라우트를 건다.
PAGES = {PATH: character_page}
