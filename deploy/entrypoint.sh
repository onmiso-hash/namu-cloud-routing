#!/usr/bin/env bash
# NAMU 공용 라우팅 MCP 클라우드 컨테이너 entrypoint.
#
# 1) NAMU_STORE_REMOTE(토큰 내장 HTTPS remote URL)로 NAMU_STORE_ROOT를 clone,
#    이미 있으면 pull.
# 2) git identity(user.email/user.name)가 비어 있으면 기본값을 채운다
#    (vendor/namu-agent/deploy/entrypoint.sh 방식 그대로 — pull이 머지커밋을
#    만들 때 identity가 없으면 실패하는 실배포 갭 대비).
# 3) src/routing_server.py를 exec로 기동한다(PID 1 시그널을 그대로 전달하기
#    위해 exec 사용).
#
# 범위 밖(후속): sync_setup 호출, 자동 push-back, 자동 pull/push 스케줄,
# 동시성 처리. 이번엔 clone/pull만 한다.
#
# 실패를 조용히 삼키지 않는다 — 각 단계는 실패 시 원인을 stderr에 남기고 즉시
# 비정상 종료(exit != 0)한다("완료" 오출력 금지).
set -euo pipefail

if [ -z "${NAMU_STORE_REMOTE:-}" ]; then
  echo "[namu-routing-entrypoint] ERROR: NAMU_STORE_REMOTE 환경변수가 설정되지 않았습니다." >&2
  echo "  예: NAMU_STORE_REMOTE=https://x-access-token:<PAT>@github.com/<user>/<repo>.git" >&2
  exit 1
fi

if [ -z "${NAMU_STORE_ROOT:-}" ]; then
  echo "[namu-routing-entrypoint] ERROR: NAMU_STORE_ROOT 환경변수가 설정되지 않았습니다." >&2
  echo "  컨테이너 내 STORE clone 경로를 지정하세요 (예: NAMU_STORE_ROOT=/data)." >&2
  exit 1
fi

if [ -d "${NAMU_STORE_ROOT}/.git" ]; then
  echo "[namu-routing-entrypoint] 기존 STORE 발견 — git pull: ${NAMU_STORE_ROOT}"
  if ! git -C "${NAMU_STORE_ROOT}" pull --no-rebase --no-edit; then
    echo "[namu-routing-entrypoint] ERROR: STORE git pull 실패 (원격/자격증명을 확인하세요)" >&2
    exit 1
  fi
else
  echo "[namu-routing-entrypoint] STORE 없음 — clone: ${NAMU_STORE_ROOT}"
  if ! git clone "${NAMU_STORE_REMOTE}" "${NAMU_STORE_ROOT}"; then
    echo "[namu-routing-entrypoint] ERROR: STORE git clone 실패 (NAMU_STORE_REMOTE/토큰을 확인하세요)" >&2
    exit 1
  fi
fi

# git identity 기본값 채우기 — 이미 설정돼 있으면(이미지를 확장해 사용자가 직접
# 넣은 경우 등) 절대 덮어쓰지 않는다 — email/name을 각각 독립적으로 부재 시에만
# 채운다.
if ! git config --global --get user.email > /dev/null 2>&1; then
  GIT_EMAIL="${NAMU_GIT_EMAIL:-namu@container}"
  git config --global user.email "${GIT_EMAIL}"
  echo "[namu-routing-entrypoint] git identity: user.email 미설정 — 기본값 적용(${GIT_EMAIL})"
fi
if ! git config --global --get user.name > /dev/null 2>&1; then
  GIT_NAME="${NAMU_GIT_NAME:-namu-cloud-routing}"
  git config --global user.name "${GIT_NAME}"
  echo "[namu-routing-entrypoint] git identity: user.name 미설정 — 기본값 적용(${GIT_NAME})"
fi

echo "[namu-routing-entrypoint] routing_server.py 기동"
exec python src/routing_server.py
