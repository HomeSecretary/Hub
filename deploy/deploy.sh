#!/usr/bin/env bash
# NAS에서 실행되는 허브 빌드·배포 스크립트.
# Jenkins가 SSH로 소스(git archive)와 .env를 올린 뒤 이 스크립트를 부른다.
#
#   deploy.sh test     테스트 이미지를 빌드해서 pytest 실행 (모든 브랜치)
#   deploy.sh deploy   실행 이미지 빌드 → compose up → 헬스체크 → 실패 시 직전 이미지로 되돌림
#
# 환경 변수:
#   IMAGE_TAG                 필수. 커밋 해시 앞 12자리
#   SECRETARY_DATA_DIR_HOST   데이터 폴더 (기본 /volume1/docker/secretary/data)
#   DOCKER                    docker 명령 (기본 "docker", 권한이 없으면 "sudo docker")
set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"   # DSM의 비대화형 SSH는 /usr/local/bin 이 빠져 있다

MODE="${1:?사용법: deploy.sh test|deploy}"
: "${IMAGE_TAG:?IMAGE_TAG 가 필요합니다}"
DOCKER="${DOCKER:-docker}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${SECRETARY_DATA_DIR_HOST:-/volume1/docker/secretary/data}"
ENV_FILE="$SRC_DIR/.env"
DEPLOY_ENV="$SRC_DIR/.deploy.env"
KEEP_IMAGES=5

PROJECT="secretary-hub"   # compose 프로젝트 이름. 모듈 저장소와 겹치지 않게 한다
NAME="secretary"          # 이미지·컨테이너 이름

# 비밀값이 든 파일은 끝나면 무조건 지운다. 컨테이너는 만들어질 때 값을 이미 받아 둔다.
cleanup() { rm -f "$ENV_FILE" "$DEPLOY_ENV"; }
trap cleanup EXIT

log() { echo "[$NAME] $*"; }

# Container Manager 버전에 따라 'docker compose' 또는 'docker-compose'
compose() {
    if $DOCKER compose version >/dev/null 2>&1; then
        $DOCKER compose -p "$PROJECT" "$@"
    else
        ${DOCKER%docker}docker-compose -p "$PROJECT" "$@"
    fi
}

# 허브와 모듈이 같이 쓰는 네트워크. 어느 저장소가 먼저 배포되어도 되게, 없으면 만든다.
ensure_networks() {
    $DOCKER network inspect secretary_edge >/dev/null 2>&1    || $DOCKER network create secretary_edge >/dev/null
    $DOCKER network inspect secretary_modules >/dev/null 2>&1 || $DOCKER network create --internal secretary_modules >/dev/null
    $DOCKER network inspect secretary_egress >/dev/null 2>&1  || $DOCKER network create secretary_egress >/dev/null
}

# compose에 넘길 값은 파일로 만든다. 'sudo docker'를 쓰면 셸 환경 변수가 넘어가지 않기 때문이다.
write_deploy_env() {
    local tag="$1"
    ( umask 077
      { cat "$ENV_FILE"; echo; echo "IMAGE_TAG=$tag"; echo "SECRETARY_DATA_DIR_HOST=$DATA_DIR"; } > "$DEPLOY_ENV" )
}

# 컨테이너가 healthy가 될 때까지 최대 60초 기다린다. 아니면 로그를 보여 주고 실패.
wait_healthy() {
    local status
    for _ in $(seq 1 30); do
        status="$($DOCKER inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo missing)"
        [ "$status" = "healthy" ] && return 0
        sleep 2
    done
    log "헬스체크 실패. 최근 로그:"
    $DOCKER logs --tail 50 "$NAME" 2>&1 || true
    return 1
}

# 데이터 폴더에 허브 컨테이너 사용자(UID 10001)가 쓸 수 있는지 미리 확인한다.
# 못 쓰면 컨테이너는 뜨자마자 'unable to open database file'로 죽고, 첫 배포에는 되돌릴 이미지도 없다.
# 방금 빌드한 허브 이미지를 그대로 써서 확인한다 (같은 UID, 따로 받을 이미지 없음).
check_data_dir() {
    local image="$1"
    if $DOCKER run --rm --network none --entrypoint python -v "$DATA_DIR:/data" "$image" \
        -c "import os; p='/data/.write-test'; open(p, 'w').close(); os.remove(p)" >/dev/null 2>&1; then
        return 0
    fi
    log "데이터 폴더에 UID 10001이 쓸 수 없습니다: $DATA_DIR"
    log "  현재 소유자 UID: $(stat -c %u "$DATA_DIR" 2>/dev/null || echo '?')  (ls -ldn 결과 세 번째 칸)"
    log "  고치기: sudo chown 10001 '$DATA_DIR' && sudo chmod 700 '$DATA_DIR'"
    log "  Synology에서 권한 끝에 '+'가 보이면 ACL 상속 때문입니다. File Station → 폴더 속성 → 권한에서 상속을 끄고 다시 chown 하세요."
    return 1
}

cd "$SRC_DIR"

case "$MODE" in
test)
    log "테스트: $NAME-test:$IMAGE_TAG"
    $DOCKER build --target test -t "$NAME-test:$IMAGE_TAG" .
    $DOCKER rmi "$NAME-test:$IMAGE_TAG" >/dev/null 2>&1 || true
    log "테스트 통과"
    ;;

deploy)
    [ -s "$ENV_FILE" ] || { log ".env 가 없습니다. Jenkins Credentials 'secretary-env'를 확인하세요."; exit 1; }
    [ -d "$DATA_DIR" ] || { log "데이터 폴더가 없습니다: $DATA_DIR"; exit 1; }

    log "빌드: $NAME:$IMAGE_TAG"
    $DOCKER build --target runtime -t "$NAME:$IMAGE_TAG" .

    # 지금 돌고 있는 컨테이너는 건드리기 전에 확인한다. 실패하면 기존 서비스는 그대로 둔다.
    check_data_dir "$NAME:$IMAGE_TAG" || exit 1

    PREV_IMAGE="$($DOCKER inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null || true)"
    PREV_TAG=""
    [[ "$PREV_IMAGE" == "$NAME":* ]] && PREV_TAG="${PREV_IMAGE#"$NAME":}"
    log "이전 태그: ${PREV_TAG:-(없음)}"

    ensure_networks
    write_deploy_env "$IMAGE_TAG"
    log "배포"
    compose --env-file "$DEPLOY_ENV" up -d --no-build --remove-orphans

    if ! wait_healthy; then
        if [ -n "$PREV_TAG" ] && [ "$PREV_TAG" != "$IMAGE_TAG" ]; then
            log "$PREV_TAG 로 되돌립니다"
            write_deploy_env "$PREV_TAG"
            compose --env-file "$DEPLOY_ENV" up -d --no-build
        fi
        exit 1
    fi
    log "배포 완료: $IMAGE_TAG"

    # 되돌리기용으로 최근 이미지 몇 개만 남긴다.
    $DOCKER images "$NAME" --format '{{.CreatedAt}}|{{.Repository}}:{{.Tag}}' \
        | sort -r | tail -n +$((KEEP_IMAGES + 1)) | cut -d'|' -f2 \
        | while read -r img; do $DOCKER rmi "$img" >/dev/null 2>&1 || true; done
    $DOCKER image prune -f >/dev/null 2>&1 || true
    ;;

*)
    echo "사용법: deploy.sh test|deploy" >&2
    exit 2
    ;;
esac
