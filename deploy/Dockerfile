# NAMU 공용 라우팅 MCP 서비스 배포 이미지 (namu-cloud-routing).
#
# 빌드 (repo 루트에서, 컨텍스트=repo 루트여야 vendor/namu-agent/namu-plugin을 COPY할 수 있다):
#   docker build -f deploy/Dockerfile -t namu-cloud-routing .
#
# 실행 예 (시크릿은 이미지에 굽지 않고 런타임 -e로 주입):
#   docker run -p 8770:8770 \
#     -e NAMU_STORE_ROOT=/data \
#     -e NAMU_HTTP_TOKEN="<임의의 강한 토큰>" \
#     -v namu_store:/data \
#     namu-cloud-routing
FROM python:3.12-slim

# uv 공식 배포 이미지에서 바이너리만 복사한다(curl|sh 파이프보다 재현성/신뢰성이 높음).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# git: vendor submodule 관련 도구 체인 호환성 목적. ca-certificates: HTTPS 통신.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 프로젝트 메타데이터(패키지 자체는 설치하지 않음 — 런타임 의존성만 system python에 설치).
COPY pyproject.toml uv.lock ./

# 라우팅 서버 본체.
COPY src/ ./src/

# entrypoint(STORE clone/pull → routing_server.py exec 기동).
# CRLF 제거: Windows checkout(core.autocrlf)이 .sh를 CRLF로 바꾸면 리눅스
# bash가 'set -euo pipefail\r'을 파싱 못 해 크래시한다. .gitattributes로 LF를
# 고정하지만, 클론 환경이 이를 무시해도 안전하도록 빌드 단계에서 CR을 한 번 더 제거.
COPY deploy/entrypoint.sh ./deploy/entrypoint.sh
RUN sed -i 's/\r$//' /app/deploy/entrypoint.sh && chmod +x /app/deploy/entrypoint.sh

# 메모리 코어(config.py/db.py/profile.py 등)는 git submodule을 sys.path로 재사용한다.
# routing_server.py가 `Path(__file__).resolve().parent.parent / "vendor" / "namu-agent" / "namu-plugin"`
# 를 sys.path에 얹으므로, 이 이미지에서도 동일한 상대 경로(/app/vendor/namu-agent/namu-plugin)가
# 보존되도록 COPY 대상 경로를 그대로 맞춘다. vendor/namu-agent 전체가 아니라 namu-plugin 하위만
# 복사한다(수정 금지 대상이므로 원본 그대로).
COPY vendor/namu-agent/namu-plugin/ ./vendor/namu-agent/namu-plugin/

# 런타임 의존성 4개만 system python에 설치한다(pyproject.toml에 [build-system]이 없어
# `uv pip install .`는 실패할 수 있으므로 프로젝트 패키지 자체는 설치하지 않는다).
RUN uv pip install --system \
    "mcp[cli]>=1.28,<2" \
    "python-ulid>=3.0.0" \
    "PyYAML>=6.0" \
    "python-dotenv>=1.0.0"

# 컨테이너는 0.0.0.0으로 바인드해야 리버스 프록시가 도달할 수 있다
# (127.0.0.1 바인드는 컨테이너 내부 루프백만 수신 — 외부에서 절대 도달 불가).
ENV NAMU_HTTP_HOST=0.0.0.0
ENV NAMU_HTTP_PORT=8770

# NAMU_STORE_ROOT / NAMU_HTTP_PATH_SECRET / NAMU_HTTP_TOKEN 등 시크릿은 의도적으로
# 기본값을 두지 않는다 — 이미지에 굽지 않고 배포 플랫폼 환경변수로만 런타임 주입한다.

EXPOSE 8770

CMD ["bash", "/app/deploy/entrypoint.sh"]
