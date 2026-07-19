# NAMU 공용 클라우드 MCP 사용 가이드 (경로 A — 중앙 호스팅·멀티유저)

> 📅 2026-07-19(namu-54) · 선행 문서: [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md)(경로 B 셀프호스팅 가이드) · [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md)(설계 원본, §11에 경로 A 미래설계).
>
> **범위** — 경로 A는 "중앙에서 우리가 대신 호스팅해주는 공용 서버에 접속만 하면 되는" 형태다(사용자가 직접 서버를 띄우는 경로 B와 반대). **현재는 소수 신뢰 사용자 체험용**이며 일반 공개 서비스가 아니다 — 이유는 5·6절 인증 한계를 볼 것.

## 1. 이게 뭔가 — 경로 B(셀프호스팅)와 차이

| | 경로 B(셀프호스팅) | 경로 A(공용 클라우드, 이 문서) |
|---|---|---|
| 서버는 누가 띄우나 | 사용자 본인이 직접 | 중앙(onnamu.kr)이 대신 호스팅 |
| 사용자 수 | 단일 사용자(자기 것 하나) | 멀티유저(`?user=` 키로 사용자별 서랍 라우팅) |
| 데이터 노출 범위 | 자기 `~/.namu` 하나만 | 중앙 저장소 안에서 `?user=` 키별 폴더로 분리 |

라이브 사실 — 공용 서비스는 `namu-cloud-routing:v0.1.6` 이미지(내부 포트 8770, `namu_cloud_store` named volume)로 떠 있고, Cloudflare named tunnel(고정 도메인으로 상시 연결되는 터널)을 통해 `namu-cloud.onnamu.kr`에 연결돼 있다. 노출 도구는 개인용(경로 B)과 동일한 3종 `namu_recall`/`namu_record`/`namu_search`다 — `namu_sync_setup`과 tasks는 노출되지 않는다(경로 B와 같은 이유, [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md) 1절 참고).

포트·이미지 태그·Cloudflare ingress 같은 인프라 세부는 onnamu-project/specs 관할이라 이 문서에서는 요약만 다루고 중복 관리하지 않는다.

## 2. 연결법

접속 URL 형식은 다음과 같다.

```
https://namu-cloud.onnamu.kr/mcp/<PATH_SECRET>?user=<내-키>&client=<AI-이름>
```

- `<PATH_SECRET>` — 운영자가 배포한 공유 시크릿 경로. 사용자에게 별도로 전달된다.
- `?user=`·`&client=` — 필수 쿼리(각각 3절·4절에서 설명).

등록 방법은 두 가지다.

- **claude.ai 커스텀 커넥터** — 설정 → Connectors → Add custom connector에 위 URL을 통째로 입력한다([`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md) 3-3절과 동일한 절차).
- **Claude Code CLI**
  ```bash
  claude mcp add --transport http namu-cloud \
    "https://namu-cloud.onnamu.kr/mcp/<PATH_SECRET>?user=<키>&client=<AI>"
  ```

## 3. `user`·`client`가 뭔가

- **`user`** — 내 서랍(폴더) 이름표다. 서버가 `?user=<키>`를 받아 `STORE_ROOT/users/<키>/`로 라우팅한다. 같은 키를 쓰면 같은 기억 풀을 공유하고, 다른 키를 쓰면 서로 다른 서랍이 된다.
- **`client`** — 이 기억을 남긴 AI가 누구인지 나타내는 출처 태그(내부적으로 `via`로 저장). record 시 함께 저장돼, 나중에 "claude가 남긴 이력만" 같은 출처별 조회의 기준이 된다.

역할을 한 줄로 요약하면: `user`=어느 서랍, `client`=어느 AI.

## 4. `user`·`client` 입력 규칙 (중요)

실코드로 검증된 형식은 다음과 같다.

- `user` 키 — 영숫자·`-`·`_` 1~64자(`^[A-Za-z0-9_-]{1,64}$`).
- `client` 값 — 영숫자·`.`·`_`·`-` 1~40자(`^[A-Za-z0-9._-]{1,40}$`).

슬래시·공백·`..` 등 경로를 벗어나려는 문자는 거부된다.

**3개 도구(recall/record/search) 모두 `user`·`client` 둘 다 필수**다 — 하나라도 없거나 형식이 틀리면 도구 호출이 한국어+영어 상세 에러로 거부된다(에러 안내가 뜨니, URL 쿼리를 고쳐서 다시 붙이면 된다).

`client` 이름은 **정확한 모델명 예시(`claude` / `chatgpt` / `gemini` / `cursor` / `copilot`)로 넣기를 권장**한다. 애칭·변형도 형식만 맞으면 거부되지 않지만, **나중에 조회할 때 저장했던 값과 글자 그대로 똑같이 넣어야 찾힌다** — `claude`와 `cld`는 서로 다른 값으로 저장된다. 자기만의 애칭을 쓸 거라면 매번 일관되게 쓸 것.

## 5. 인증·격리 모델 (현재 — 반드시 읽을 것)

현재 인증은 **공유 `path_secret` 1개뿐**이다. `?user=<키>`는 인증이 아니라 **폴더 이름표(라우팅 라벨)**일 뿐이다.

따라서 공유 시크릿 경로를 아는 사람은, 남의 `user` 키만 알아내면 그 서랍도 열 수 있다 — 이건 서로 협조한다는 전제 위의 **협조적 격리**이지, 암호학적으로 잠긴 접근 통제가 아니다. (경로 이탈 문자는 `_validate_user_key`/`_paths_for_user`가 막아주지만, "남의 키를 그대로 지정"하는 행위 자체는 막지 못한다.)

그래서 현재 공용 서버는 **서로 신뢰하는 소수 체험용**이며, 아무나 접속하는 일반 공개 서비스로 쓰기엔 아직 부족하다.

## 6. 지금 안 되는 것 / 나중 계획

- **미구현: 사용자별 인증** — 각 사용자가 자기 서랍에만 접근하도록 암호학적으로 잠그는 기능. 이게 갖춰져야 일반 공개가 가능하다.
- **방향** — OAuth 사용자별 토큰으로 전환할 예정이다. 상세 설계는 [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md) §11(이번에 함께 갱신됨)을 참고할 것.

## 관련 문서

- [`remote_mcp_guide.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_guide.md) — 경로 B(셀프호스팅) 사용 가이드
- [`remote_mcp_design.md`](https://github.com/onmiso-hash/namu-agent/blob/main/docs/remote_mcp_design.md) — 설계 원본, §11에 경로 A 현재 상태·OAuth 미래설계
