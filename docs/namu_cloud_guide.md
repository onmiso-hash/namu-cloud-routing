# NAMU 공용 클라우드 MCP 사용 가이드 (경로 A — 중앙 호스팅·멀티유저)

> 📅 2026-07-31 개정(namu-60) · 최초 작성 2026-07-19(namu-54) · 선행 문서: [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md)(경로 B 셀프호스팅 가이드) · [`install_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/install_guide.md)(플러그인 설치 가이드) · [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md)(설계 원본).
>
> **범위** — 경로 A는 "중앙에서 우리가 대신 호스팅해주는 공용 서버에 접속만 하면 되는" 형태다(사용자가 직접 서버를 띄우는 경로 B와 반대).
>
> **이 문서의 개정 이력(중요)** — 2026-07-19 최초본은 ⓐ 접속 주소가 전원 공용 시크릿 + `?user=<내 키>` 형식이고 ⓑ 그 주소를 로컬 Claude Code에도 `claude mcp add`로 붙이라고 안내했다. **둘 다 지금은 틀린 설명이다.** ⓐ는 namu-59에서 사용자별 개인 열쇠로 바뀌었고(전원 공용 열쇠는 인증이 아니었다 — 5절), ⓑ는 애초에 잘못된 안내였다(2절). 옛 문서를 보고 설정한 사용자는 2절을 다시 읽고 고쳐 잡을 것.

## 1. 이게 뭔가 — 경로 B(셀프호스팅)와 차이

| | 경로 B(셀프호스팅) | 경로 A(공용 클라우드, 이 문서) |
|---|---|---|
| 서버는 누가 띄우나 | 사용자 본인이 직접 | 중앙(onnamu.kr)이 대신 호스팅 |
| 사용자 수 | 단일 사용자(자기 것 하나) | 멀티유저(주소에 실린 **개인 열쇠**로 사용자별 서랍 라우팅) |
| 기억의 원본은 어디에 | 자기 PC의 `~/.namu` | **사용자 본인의 GitHub 저장소**(연결 시 직접 고른다). 서버가 갖는 것은 그 저장소의 사본뿐이다 |

노출 도구는 개인용(경로 B)과 동일한 3종 `namu_recall`/`namu_record`/`namu_search`이고, 기록은 개인용과 같은 3층(요약 `summary` · 왜 `reason` · 원문 `body`)으로 남는다(namu-68). 담을 수 있는 그릇은 교훈(learnings)·개인 사실(profile)·쪽지(memo) 셋이며 — `namu_sync_setup`과 tasks는 노출되지 않는다(경로 B와 같은 이유, [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md) 1절 참고).

포트·이미지 태그·Cloudflare ingress 같은 인프라 세부는 onnamu-project/specs 관할이라 이 문서에서는 다루지 않는다(중복 관리 금지).

## 2. 연결법 — 이 주소는 **웹 AI 전용**이다

### 2-1. 먼저 알아야 할 것 (여기서 갈린다)

| 무엇을 쓰시나요 | 어떻게 하나요 |
|---|---|
| **웹 AI**(claude.ai, ChatGPT 등 브라우저에서 쓰는 AI) | 이 문서대로 **접속 주소를 커넥터에 붙인다** |
| **Claude Code · agy**(터미널에서 쓰는 AI) | 주소를 붙이는 게 아니라 **나무를 플러그인으로 설치**한다 → [`install_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/install_guide.md) |

왜 Claude Code·agy는 주소를 붙이면 안 되나 — 이 주소로 넘어가는 것은 **기억 도구 3개뿐**이다. 세션 시작 브리핑, `/namu-task` 작업 절차, 워커 호출, 마무리 훅처럼 나무의 나머지 절반은 플러그인에만 들어 있고 주소로는 따라오지 않는다. 게다가 기억이 쌓이는 자리도 갈라진다(플러그인은 그 PC의 `~/.namu`, 이 주소는 사용자 GitHub 저장소). 반쪽짜리 나무를 쓰면서 기억까지 두 곳으로 흩어지는 셈이라, 터미널 사용자에게는 권하지 않는다.

### 2-2. 접속 주소 받기

1. 브라우저로 <https://namu-cloud.onnamu.kr/auth/github/login> 에 접속해 GitHub으로 로그인한다.
2. 안내에 따라 **NAMU 앱을 설치**하고, 기억을 저장할 **내 GitHub 저장소를 하나 고른다**(비공개 저장소 권장. 없으면 그 화면에서 새로 만들 수 있다).
3. 연결이 끝나면 화면에 **접속 주소**가 그대로 뜬다. 창을 닫았어도 언제든 <https://namu-cloud.onnamu.kr/auth/me>(내 페이지)에서 다시 볼 수 있다.

주소 형식은 다음과 같다.

```
https://namu-cloud.onnamu.kr/mcp/<내-개인-열쇠>?client=<AI-이름>
```

- `<내-개인-열쇠>` — 사람마다 다른 무작위 값(namu-59). 위 절차로 자동 발급되며, 운영자가 따로 나눠주는 공용 값이 아니다.
- `?client=` — 이 기억을 남긴 AI가 누구인지 나타내는 출처 태그(3절).

> ⚠️ **이 주소는 사실상 비밀번호다.** 아는 사람은 회원님 기억을 읽고 쓸 수 있다 — 채팅·이슈·스크린샷에 그대로 붙여넣지 말 것. 새어 나갔다면 내 페이지에서 **재발급**하면 옛 주소는 즉시 막힌다(5절).

### 2-3. claude.ai에 붙이기

설정(Settings) → 커넥터(Connectors) → 사용자 정의 커넥터 추가(Add custom connector) → 위 주소를 통째로 붙여넣고 저장.

다른 웹 AI를 쓴다면 주소 끝의 `client=claude`를 그 AI 이름으로 바꿔서 붙인다(예: `client=chatgpt`).

## 3. `client`가 뭔가

- **`client`** — 이 기억을 남긴 AI가 누구인지 나타내는 출처 태그(내부적으로 `via`로 저장). record 시 함께 저장돼, 나중에 "claude가 남긴 이력만" 같은 출처별 조회의 기준이 된다.
- **`user`는 이제 사용자가 붙이지 않는다.** 예전에는 `?user=<내 키>`로 서랍 이름표를 직접 적어 보냈지만, 지금은 서버가 주소에 실린 개인 열쇠로 누구인지 판정해 **직접 채워 넣는다**(요청에 `?user=`를 손으로 실어 보내도 서버가 걷어내고 다시 쓴다). 남의 이름표를 적어 넣어 남의 서랍을 여는 수법을 없애기 위한 구조다.

입력 규칙(실코드로 검증된 형식):

- `client` 값 — 영숫자·`.`·`_`·`-` 1~40자(`^[A-Za-z0-9._-]{1,40}$`).
- **3개 도구(recall/record/search) 모두 `client`가 필수**다 — 없거나 형식이 틀리면 도구 호출이 한국어+영어 상세 에러로 거부된다(주소 끝에 `?client=<AI 이름>`을 붙여 다시 등록하면 된다).

`client` 이름은 **정확한 모델명 예시(`claude` / `chatgpt` / `gemini` / `cursor` / `copilot`)로 넣기를 권장**한다. 애칭·변형도 형식만 맞으면 거부되지 않지만, **나중에 조회할 때 저장했던 값과 글자 그대로 똑같이 넣어야 찾힌다** — `claude`와 `cld`는 서로 다른 값으로 저장된다.

## 4. 저장소를 아직 연결하지 않았다면

로그인만 하고 저장소를 고르지 않은 상태에서는 도구 호출이 "먼저 로그인하고 저장소를 연결하라"는 안내와 함께 거부된다(빈 기억이 조용히 만들어지지 않는다). 2-2절 절차를 끝까지 밟을 것.

## 5. 인증·격리 모델 (현재)

- **인증은 주소에 실린 개인 열쇠 하나**다(namu-59). 서버는 요청마다 그 열쇠를 장부에서 찾아 "누구의 서랍인가"를 판정하고, 못 찾으면 404로 끊는다.
- 따라서 **주소를 아는 것 = 그 사람 본인으로 인정받는 것**이다. 남에게 알려주지 말 것.
- **주소가 새어 나갔거나 그만 쓰고 싶다면** — 내 페이지(<https://namu-cloud.onnamu.kr/auth/me>)에서 직접 처리한다(namu-60).
  - **연결 시험** — 지금 이 주소가 실제로 동작하는지 서버가 대신 확인해 준다. 판정은 `살아있음` / `주소가 잘못됨` / `지금은 확인 불가` 셋이며, 마지막은 **주소가 잘못됐다는 뜻이 아니다**(서버 재시작 직후 등 일시적 상황) — 1~2분 뒤 다시 눌러보면 된다.
  - **재발급** — 새 주소를 만든다. 옛 주소는 그 순간 막히므로, AI에 등록해 둔 커넥터 주소도 새것으로 바꿔야 한다.
  - **폐기** — 주소를 아예 없앤다(확인 단계를 한 번 거친다). 기억 자체는 회원님 GitHub 저장소에 그대로 남으므로, 나중에 재발급하면 이어서 쓸 수 있다.
- **기억의 원본은 회원님 GitHub 저장소**다(namu-58). 나무는 회원님이 앱 설치 때 직접 고른 저장소 한 칸에만 유효한 단명 토큰(1시간)으로 접근하며, 그 저장소의 사본을 서버에 두고 읽고 쓴 뒤 다시 밀어 올린다.

### 예전 모델(폐기됨)과의 차이

2026-07-19 최초본이 설명한 "공유 `path_secret` 1개 + `?user=` 이름표"는 **인증이 아니었다** — 공유 시크릿을 아는 사람은 남의 `user` 키만 알아내면 그 서랍을 열 수 있었다(요청자가 스스로 밝히는 값을 그대로 믿는 구조). namu-59에서 사용자별 개인 열쇠로 바뀌면서 이 결함은 사라졌고, `NAMU_HTTP_PATH_SECRET`(전원 공용 열쇠)은 설정돼 있어도 아무 효과가 없다.

## 6. 지금 안 되는 것 / 나중 계획

- 접속 주소는 사람이 브라우저로 받아 손으로 붙이는 방식이다. OAuth로 AI 클라이언트가 직접 인증을 받는 형태(동적 클라이언트 등록)는 아직 아니다 — 상세는 [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md) §11 참고.
- tasks(작업 기록)·`namu_sync_setup`은 이 경로에 노출되지 않는다. `namu_record`에 `bowl='tasks'`로 부르면 조용히 다른 그릇에 담지 않고 거절한다 — 작업 기록은 코어가 PC별 위치(홈 폴더)에 쓰는 데이터라 사용자별로 갈라지지 않기 때문이다(namu-68).
- `namu_search`는 아직 교훈(learnings) 그릇만 찾는다. 개인용 서버의 `bowl=` 축(개인 사실·쪽지 검색)은 이 경로에 아직 없다 — 저장·조회는 되므로(`namu_recall`이 세 그릇을 다 돌려준다) 기록이 유실되지는 않는다.

## 관련 문서

- [`install_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/install_guide.md) — Claude Code·agy용 플러그인 설치 가이드(터미널 사용자는 이쪽)
- [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md) — 경로 B(셀프호스팅) 사용 가이드
- [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md) — 설계 원본
