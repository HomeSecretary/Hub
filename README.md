# Secretary Hub

집에서 돌리는 가족용 비서의 허브. NAS의 Docker에서 돌고, Cloudflare Tunnel과 Access 뒤에 숨어 있다.
여러 클라이언트(폰 앱, 스마트 안경, 워치, MCP)의 요청을 받아 대신 처리한다.
한 집에서 최대 4명(설정으로 변경 가능)이 함께 쓰는 것을 전제로 만들었다.

## 구조: 허브 + 모듈

```
                                                        ┌─ secretary-mail   (AWS SES)
클라이언트 ─HTTPS→ Cloudflare Access ─터널→ cloudflared ─→ secretary (허브) ─┼─ secretary-tapo   (예정)
 HMAC 서명                                   (NAS, 인바운드 포트 0개)     ├─ secretary-sms    (예정)
                                                                          ├─ secretary-kakao  (예정)
                                                                          └─ secretary-summary(예정)
```

- **허브(이 저장소)**: 사용자·기기 키, HMAC 인증, 권한, 승인 대기열, 감사 로그, 명령 전달. 모듈이 무슨 일을 하는지는 모른다.
- **모듈(모듈마다 별도 저장소, 예: [Mail](https://github.com/HomeSecretary/Mail))**: 각자 컨테이너. 입력 검사와 실제 실행, 외부 서비스 키를 맡는다. 허브만 부를 수 있다.
- 허브와 모듈은 HTTP 약속(계약)으로만 이어진다. 서로의 코드를 import 하지 않는다. → [docs/module-contract.md](docs/module-contract.md)
- 모듈을 추가할 때 허브 코드는 고치지 않는다. 컨테이너 하나와 설정 한 줄이면 된다.

## 지금 되는 것
- **가족 단위 사용자**: 관리자(admin), 어른 가족(member), 아이(child) 역할. 기기는 사람에게 속한다
- **HMAC 요청 서명 인증**: 기기별 키, 타임스탬프 ±60초, Nonce 재사용 차단, 실패가 몰리면 키 잠금
- **권한**: 기기에 준 scope AND 역할이 허용하는 scope. 모듈 권한은 `<모듈>:<액션>`, 와일드카드 `mail:*`, `modules:*`
- **하나의 명령 입구**: `POST /v1/commands {"module", "action", "params"}`. 모듈이 늘어도 클라이언트 API는 그대로
- **다른 기기 승인**: 모듈이 manifest에 `"approval": true`로 적은 액션은 승인 대기열로
  - 관리자, 어른 가족: 본인의 **다른** 기기에서 승인
  - 아이: **관리자**의 기기에서 승인
  - 다른 가족의 요청은 보이지 않고 승인할 수도 없다 (없는 요청과 똑같이 404)
- **이벤트 콜백**: 모듈 → 허브 `POST /internal/events`, 클라이언트는 `GET /v1/events`
- **모듈**: 메일(AWS SES)은 [Mail](https://github.com/HomeSecretary/Mail) 저장소. 허브는 모듈이 무슨 일을 하는지 모르고 manifest만 본다
- **감사 로그**: `data/audit.log` (JSONL). 누가(사람, 기기) 무엇을 했는지. 비밀값과 명령 입력값은 남기지 않는다

## 저장소 구성
```
app/              허브 (FastAPI)
tests/            테스트. 가짜 모듈(tests/fake_module.py)로 계약을 확인한다
scripts/          예제 클라이언트 (표준 라이브러리만)
docs/             모듈 계약
deploy/deploy.sh  NAS에서 도는 빌드·배포 스크립트
Dockerfile, docker-compose.yml, Jenkinsfile
```
모듈은 저장소가 따로 있다 (예: [Mail](https://github.com/HomeSecretary/Mail)). 허브와 모듈은 [모듈 계약](docs/module-contract.md)으로만 이어진다.

## API (허브)
| 메서드 | 경로 | scope | 설명 |
|---|---|---|---|
| GET | `/healthz` | 없음 | 상태 확인 (Docker healthcheck용) |
| GET | `/v1/me` | `system:read` | 내 사용자, 기기, 실제 권한 |
| GET | `/v1/modules` | `system:read` | 이 기기로 쓸 수 있는 모듈과 액션 (메뉴 그리기용) |
| POST | `/v1/commands` | `<모듈>:<액션>` | 명령. 승인이 필요하면 202 + 승인 대기, 아니면 200 + 결과 |
| GET | `/v1/approvals?status=pending` | `approvals:decide` | 내가 결정할 수 있는 요청 목록 (요약만) |
| POST | `/v1/approvals/{id}/approve` | `approvals:decide` | 승인하면 모듈이 실행 |
| POST | `/v1/approvals/{id}/reject` | `approvals:decide` | 거절 |
| GET | `/v1/events?after=<id>` | `system:read` | 나와 가족 모두에게 온 이벤트 |
| POST | `/internal/events` | 모듈 토큰 | 모듈 → 허브. 터널을 거친 요청은 404 |

명령 예:
```json
POST /v1/commands
{"module": "mail", "action": "send", "params": {"to": ["grandma@example.com"], "subject": "퇴근", "body": "지금 출발해요"}}
```

오류 응답은 항상 `{"code": "...", "message": "..."}` 형식이다. 인증 실패 원인은 응답에 담지 않고 감사 로그에만 남긴다.

## 역할과 기기 프리셋
| 역할 | 쓸 수 있는 scope | 승인 |
|---|---|---|
| `admin` | system:read, approvals:decide, modules:* | 본인 요청(다른 기기로) + 아이 요청 |
| `member` | system:read, approvals:decide, modules:* | 본인 요청(다른 기기로) |
| `child` | system:read, modules:* | 못 함. 승인이 필요한 요청은 관리자가 승인 |

모듈 액션마다 역할을 더 좁히고 싶으면 모듈 manifest의 `roles`에 적는다 (예: 현관문 열기는 `["admin"]`).

| 프리셋 | scope | 용도 |
|---|---|---|
| `phone` | system:read, approvals:decide, modules:* | 메인 앱, 승인 |
| `watch` | system:read, approvals:decide | 승인 탭 |
| `glasses` | system:read, modules:* | 요청만 |
| `mcp` | system:read | AI는 일단 조회만 |
| `test` | system:read | curl 테스트 |

프리셋 대신 직접 줄 수도 있다: `--scope mail:send --scope tapo:*`

첫 사용자는 반드시 `admin`이어야 하고, 마지막 관리자는 비활성화할 수 없다.

## 요청 서명
```
서명할 문자열 = METHOD \n PATH[?QUERY] \n TIMESTAMP \n NONCE \n SHA256(body) 16진수
X-Signature  = base64url(HMAC-SHA256(secret, 서명할 문자열)), 끝의 '=' 제거
```
헤더: `X-Access-Key`, `X-Timestamp`(유닉스 초), `X-Nonce`(16~64자 영숫자), `X-Signature`.
Cloudflare Access 서비스 토큰 헤더(`CF-Access-Client-Id`, `CF-Access-Client-Secret`)도 함께 보낸다.
구현 예시는 `scripts/secretary_client.py` (표준 라이브러리만 사용).

## 준비물
- Docker가 돌아가는 NAS 또는 홈 서버 (Synology Container Manager 기준으로 작성)
- Cloudflare 계정과 도메인 (Zero Trust 무료 플랜으로 충분)
- 모듈마다 필요한 것은 각 모듈 저장소의 README 참고 (메일 모듈은 AWS SES)
- 자동 배포를 하려면 Gitea와 Jenkins (선택. 없으면 아래 "손으로 배포" 참고)

## Cloudflare 설정 요약
1. Zero Trust → 네트워크 → Tunnels에서 터널을 만들고 토큰을 받는다 (`.env`의 `TUNNEL_TOKEN`)
2. Access → 서비스 자격 증명에서 기기별 서비스 토큰을 만든다
3. Access → 애플리케이션 → 공개 DNS(Self-hosted)로 `api.<도메인>`을 등록하고, 정책 작업은 **서비스 인증**
4. 터널 경로: `api.<도메인>` → `http://secretary:8080`, Access JWT 검증 켜기 (팀 이름 + AUD 태그)

## 배포 (Gitea → Jenkins → NAS)
```
git push → Gitea → webhook → Jenkins (docker 없음)
   → SSH로 소스를 NAS에 올림 (git archive)
   → NAS가 직접: 테스트 → 이미지 빌드(태그=커밋 해시) → docker compose up → 헬스체크
   → 실패하면 직전 이미지로 자동 되돌림
```
Jenkins 컨테이너에는 docker를 넣지 않는다. docker.sock을 가진 컨테이너는 NAS의 root와 같은 권한이라,
Jenkins가 털리면 NAS 전체가 털린다. 대신 Jenkins는 배포 전용 계정으로 SSH만 할 수 있다.
Gitea와 Jenkins는 터널에 연결하지 않는다.

허브와 모듈은 저장소마다 Jenkins 작업이 따로 있고 따로 배포된다. 하나를 고쳐도 다른 것은 다시 배포하지 않는다.

NAS 폴더 구성 (기본값, `SECRETARY_NAS_BASE`로 변경 가능):
```
/volume1/docker/secretary/
├── src/
│   ├── hub/   이 저장소. 배포할 때마다 Jenkins가 새로 올린다 (.env는 배포 중에만 잠깐 생겼다 지워진다)
│   └── mail/  메일 모듈 저장소 (모듈마다 하나씩)
└── data/      registry.json(사용자·기기 키), SQLite, 감사 로그. 배포와 상관없이 유지된다
```

Docker 네트워크 세 개(`secretary_edge`, `secretary_modules`(internal), `secretary_egress`)는 허브와 모듈이 같이 쓴다.
어느 저장소가 먼저 배포되어도 되도록 각 `deploy.sh`가 없으면 만든다.

### 처음 한 번만
1. **배포 전용 계정** (DSM 제어판 → 사용자)
   - `deploy` 계정을 만들고, 배포 폴더에만 읽기/쓰기 권한을 준다.
   - 제어판 → 터미널에서 SSH를 켠다 (공유기에서 포트는 열지 않는다).
   - docker 실행 권한: `deploy`를 `docker` 그룹에 넣거나(`/var/run/docker.sock` 그룹 소유가 docker여야 한다),
     sudo가 필요하면 Jenkins 환경 변수 `SECRETARY_NAS_DOCKER="sudo docker"`로 두고 sudoers에 docker만 허용한다.
     ```
     deploy ALL=(root) NOPASSWD: /usr/local/bin/docker, /usr/local/bin/docker-compose
     ```
     DSM 업데이트 후 docker.sock 권한이 초기화될 수 있으니, 배포가 갑자기 권한 오류로 실패하면 여기부터 확인한다.
2. **데이터 폴더** (NAS SSH)
   ```bash
   sudo mkdir -p /volume1/docker/secretary/data
   sudo chown 10001 /volume1/docker/secretary/data && sudo chmod 700 /volume1/docker/secretary/data
   ```
3. **SSH 키** (Jenkins 컨테이너 안에서 한 번)
   ```bash
   ssh-keygen -t ed25519 -N "" -f /tmp/nas-deploy -C jenkins-secretary
   # 공개키(/tmp/nas-deploy.pub)를 NAS의 /var/services/homes/deploy/.ssh/authorized_keys 에 넣는다 (권한 700/600)
   ssh-keyscan -t ed25519 <NAS내부IP> >> ~/.ssh/known_hosts   # 호스트 키 고정 (StrictHostKeyChecking=yes). Jenkins를 실행하는 사용자로
   ssh -i /tmp/nas-deploy deploy@<NAS내부IP> 'docker version --format {{.Server.Version}}'   # 접속 확인
   ```
   개인키(/tmp/nas-deploy)는 다음 단계에서 Credentials로 올린 뒤 지운다.
4. **Jenkins 플러그인과 Credentials**
   - 플러그인: **SSH Agent**
   - SSH 개인키 (SSH Username with private key): 3번의 개인키. ID는 자유롭게 정하고 5번의 `SECRETARY_SSH_CREDENTIALS`에 적는다
   - `secretary-env` (Secret file): 이 저장소의 `.env.example`을 채운 `.env` 파일. 저장소에는 절대 올리지 않는다.
     모듈 토큰은 `openssl rand -hex 32`로 모듈마다 따로 만들어, 여기(`SECRETARY_MODULE_TOKENS`)와 그 모듈의 `.env`에 같은 값을 넣는다.
   - 모듈은 저장소마다 자기 Secret file이 따로 있다 (예: `secretary-mail-env`). 허브는 모듈의 AWS 키 같은 값을 받지 않는다.
   - Gitea 클론용 계정: 이 저장소에 **읽기 권한만** 있는 Gitea 봇 계정의 토큰 (Username with password)
5. **Jenkins 환경 변수** (Manage Jenkins → System → Global properties → Environment variables, 또는 작업 설정)
   | 이름 | 필수 | 예 |
   |---|---|---|
   | `SECRETARY_NAS_HOST` | 필수 | `deploy@192.168.0.10` (접속 계정@배포 서버 주소) |
   | `SECRETARY_SSH_CREDENTIALS` | 필수 | 4번에서 만든 SSH 개인키 Credential의 ID |
   | `SECRETARY_NAS_BASE` | 선택 | 기본 `/volume1/docker/secretary` |
   | `SECRETARY_NAS_DOCKER` | 선택 | 기본 `docker`, 권한이 없으면 `sudo docker` |

   두 필수 값이 없으면 빌드가 첫 단계에서 멈추고 무엇이 빠졌는지 알려 준다.
6. **Jenkins 작업 만들기** (저장소마다 하나): 새 Item → Pipeline
   - Pipeline: *Pipeline script from SCM* → Git, 저장소 URL은 NAS 내부 주소(예: `http://gitea:3000/<계정>/secretary.git`), 브랜치 `*/main`, Script Path `Jenkinsfile`
   - Build Triggers: *Poll SCM* 체크, 스케줄은 비워 둔다 (webhook으로만 돌게)
7. **Gitea webhook**: 저장소 설정 → 웹훅 → Gitea 유형
   - URL: `http://<jenkins주소>:8080/git/notifyCommit?url=<6번의 저장소 URL>&token=<토큰>`
   - 토큰은 Jenkins *Manage Jenkins → Security → Git plugin notifyCommit access tokens*에서 만든다.
   - Gitea 기본 설정은 내부 IP로 webhook을 보내지 않는다. `app.ini`에 다음을 넣고 Gitea를 재시작한다.
     ```ini
     [webhook]
     ALLOWED_HOST_LIST = private
     ```
8. **기존 컨테이너 정리**: 같은 이름(`cloudflared`, `secretary`, `secretary-mail`)의 컨테이너를 손으로 만들어 둔 적이 있다면 먼저 지운다.
   허브와 모듈을 한 저장소(compose 프로젝트 `secretary`)로 배포하던 설치에서 옮겨 올 때도 마찬가지다.
   ```bash
   sudo docker compose -p secretary down          # 컨테이너와 네트워크만 지운다. data/ 는 그대로
   sudo rm -rf /volume1/docker/secretary/src/*    # 예전 소스 (src/hub, src/mail 로 새로 올라온다)
   ```
   허브를 먼저 배포하고, 이어서 모듈 저장소를 배포한다. 그 사이 몇 분은 메일 같은 모듈 명령이 실패한다.
9. **첫 배포**: push한다. Jenkins 빌드가 초록색이 되면 끝. 이후 수정은 git push로만 한다.

### 손으로 배포 (Gitea/Jenkins 없이)
```bash
cp .env.example .env   # 채운다
mkdir -p data && sudo chown 10001 data && chmod 700 data
docker network create secretary_edge
docker network create --internal secretary_modules
docker network create secretary_egress
docker compose -p secretary-hub up -d --build
```

### 배포가 안 될 때
처음 설치할 때 실제로 밟았던 것들이다. 코드보다 "어디에 무엇을 넣느냐"의 문제가 대부분이다.

| 증상 | 원인 | 해결 |
|---|---|---|
| `Jenkins 환경 변수를 설정하세요` | 설정값을 Credentials(Secret text)에 넣음 | Manage Jenkins → System → Global properties → Environment variables에 넣기 |
| `Could not find credentials entry with ID 'secretary-env'` | `.env` Secret file을 안 올림 (또는 Scope가 System) | Secret file, Scope **Global**, ID 정확히 `secretary-env` |
| `Host key verification failed` | Jenkins가 배포 서버의 호스트 키를 모름 | Jenkins를 실행하는 사용자로 `ssh-keyscan` → `known_hosts` |
| `The container name "/cloudflared" is already in use` | 손으로 만든 예전 프로젝트가 남아 있음 | Container Manager에서 프로젝트 중지 → 삭제 |
| `데이터 폴더에 UID 10001이 쓸 수 없습니다` / `sqlite3.OperationalError: unable to open database file` | 데이터 폴더에 UID 10001이 쓸 권한 없음 | `sudo chown 10001 <data>`, Synology는 ACL 상속(`ls -l` 끝의 `+`)도 확인 |
| `syntax error near unexpected token 'newline'` | 문서의 `<값>` 꺾쇠를 그대로 입력 | 꺾쇠 빼고 실제 값만 |
| `403 error code: 1010` | Cloudflare Browser Integrity Check가 `Python-urllib` User-Agent를 봇으로 차단 | 클라이언트 User-Agent 지정 + API 주소만 Configuration Rule로 검사 끄기 |

`deploy.sh deploy`는 컨테이너를 바꾸기 전에 방금 빌드한 허브 이미지로 데이터 폴더에 써 본다. 못 쓰면 기존 서비스는 그대로 두고 멈춘다
(첫 배포에는 되돌릴 이미지가 없어서, 이 검사가 없으면 컨테이너가 재시작만 반복한다).

## 가족 등록
Secret은 한 번만 보여 준다. 받은 Access Key와 Secret을 그 사람의 기기 앱에 넣는다.
```bash
# 사용자 (첫 사용자는 admin)
sudo docker exec -it secretary python -m app.cli user add dad "아빠" --role admin --email dad@example.com
sudo docker exec -it secretary python -m app.cli user add mom "엄마" --role member --email mom@example.com
sudo docker exec -it secretary python -m app.cli user add kid1 "첫째" --role child
sudo docker exec -it secretary python -m app.cli user list
sudo docker exec -it secretary python -m app.cli user delete <user_id>   # 오타로 만든 사용자 삭제 (아래 참고)

# 기기
sudo docker exec -it secretary python -m app.cli device add dad phone --preset phone
sudo docker exec -it secretary python -m app.cli device add dad watch --preset watch
sudo docker exec -it secretary python -m app.cli device add kid1 phone --preset phone   # 승인 권한은 역할 상한으로 잘린다
sudo docker exec -it secretary python -m app.cli device list

# 모듈 (연결 상태와, 기기에 줄 수 있는 scope 목록)
sudo docker exec -it secretary python -m app.cli module list
```
**삭제와 비활성화**: `user delete`는 기기(폐기된 것 포함)도 승인 기록도 없는 사용자만 지운다. 오타로 만든 사용자를 바로잡는 용도다.
한 번이라도 기기를 받았거나 요청한 적이 있는 사용자는 감사 로그와 승인 기록이 그 ID를 가리키고 있어서 지우지 않고, `user disable`로 비활성화한다(자리도 비워진다).

기기마다 Cloudflare Access 서비스 토큰도 따로 발급한다. 무료 플랜 한도(서비스 토큰 50개)면 가족 4명의 기기로 충분하다.

확인:
```bash
export SECRETARY_URL=https://api.<도메인>
export CF_CLIENT_ID=... CF_CLIENT_SECRET=...
export SECRETARY_ACCESS_KEY=... SECRETARY_SECRET=...
python3 scripts/secretary_client.py GET /v1/me
```

## 비상 정지
```bash
sudo docker exec -it secretary python -m app.cli device revoke <access_key>   # 기기 하나
sudo docker exec -it secretary python -m app.cli user disable <user_id>       # 사람 한 명 (기기 키 전부 폐기)
sudo docker exec -it secretary python -m app.cli device revoke-all            # 전부
```
사용자 등록, 키 발급과 폐기는 NAS 안에서만 할 수 있다. 외부 API로는 열어 두지 않았다.

## 되돌리기
NAS에 실행 이미지를 최근 5개까지 남겨 둔다(`deploy/deploy.sh`). 헬스체크 실패는 자동으로 되돌리고,
손으로 되돌릴 때는 `git revert` 후 push하는 게 가장 깔끔하다.

## 로컬 개발
허브 테스트는 가짜 모듈(`tests/fake_module.py`)로 계약을 확인한다. 진짜 모듈 없이 돈다.
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q

# 메일 모듈(옆 저장소)을 띄워서 직접 불러 보기
TOKEN=$(openssl rand -hex 32)
(cd ../mail && MAIL_MODULE_TOKEN=$TOKEN MAIL_ALLOWED_RECIPIENTS=@example.com uvicorn app.main:app --port 8081) &
SECRETARY_DATA_DIR=./data SECRETARY_REQUIRE_CF_ACCESS=false \
  SECRETARY_MODULES=mail=http://127.0.0.1:8081 SECRETARY_MODULE_TOKENS=mail=$TOKEN \
  uvicorn app.main:app --reload --port 8080
```

## 모듈 추가하기
허브 저장소는 고치지 않는다. 자세한 약속은 [docs/module-contract.md](docs/module-contract.md).
1. 새 저장소에 계약의 4개 API(`/manifest`, `/actions/{a}/validate`, `/actions/{a}`, `/healthz`)를 만든다. [Mail](https://github.com/HomeSecretary/Mail) 저장소를 본보기로.
2. 그 저장소의 `docker-compose.yml`은 `secretary_modules`(인터넷이 필요하면 `secretary_egress`도)에 external로 붙는다. `secretary_edge`에는 붙이지 않는다.
3. 그 저장소용 Jenkins 작업과 Secret file(`secretary-<이름>-env`)을 만든다.
4. 허브의 `.env`(`secretary-env`)에서 `SECRETARY_MODULES`, `SECRETARY_MODULE_TOKENS`에 한 줄씩 넣고 허브를 다시 배포한다.
5. 배포 후 `app.cli module list`로 연결을 확인하고, 기기에 scope를 준다.

## 운영 메모
- uvicorn 워커는 1개로 둔다. Nonce와 승인 대기열이 SQLite 한 파일에 있다.
- `data/registry.json`에는 기기 Secret이 원문으로 들어 있다(HMAC은 서버도 같은 비밀값이 필요하다). 권한 600, 백업할 때도 조심.
- 컨테이너는 non-root, 읽기 전용 파일시스템, 권한(capability) 전부 제거, 포트 노출 없음.
- 모듈은 `modules` 네트워크(internal)에만 있어서 바깥(cloudflared)에서 바로 닿을 수 없다. 허브만 부를 수 있다.

## 이 저장소를 가져다 쓸 때
- 저장소에는 비밀값이나 특정 집의 주소가 들어 있지 않다. 설치마다 다른 값은 전부 `.env`(Jenkins Credentials)와 Jenkins 환경 변수로 넣는다.
- 바꿀 만한 값: `SECRETARY_MAX_USERS`, `SECRETARY_NAS_BASE`
- NAS가 Synology가 아니면 `deploy/deploy.sh`의 PATH와 기본 데이터 경로만 확인하면 된다.

## 다음 단계
- Tapo 전구 모듈 (가족 공용 자원, LAN 접근 필요)
- 문자(SMS) 모듈, 카카오톡 '나에게 보내기' 모듈
- 대화 요약 모듈
- 승인 요청 실시간 알림 (SSE)

## 라이선스
MIT. [LICENSE](LICENSE) 참고.
