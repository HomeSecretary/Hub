# 모듈 계약 (Hub ↔ Module)

Secretary는 **허브 하나 + 모듈 여러 개**로 나뉜다. 허브와 모듈은 아래 약속(HTTP API)만 지키면 되고,
서로의 코드를 import 하지 않는다. 모듈은 어떤 언어로 만들어도 된다.

```
클라이언트 ──HMAC──▶ 허브 ──내부 토큰──▶ 모듈 (mail, tapo, sms, kakao, summary ...)
                      ▲                    │
                      └──── 이벤트 콜백 ────┘
```

| 하는 일 | 허브 | 모듈 |
|---|---|---|
| 사용자, 기기 키, HMAC 인증 | O | X |
| 권한(scope) 확인, 역할 상한 | O (manifest를 보고) | 선언만 |
| 승인 대기열 | O (manifest를 보고) | 선언만 |
| 감사 로그 | O | 자기 서버 로그만 |
| 입력 검사, 실제 실행, 외부 서비스 키 | X | O |

## 1. 모듈 등록

허브 설정 두 줄로 끝난다. 허브 코드는 고치지 않는다.

```
SECRETARY_MODULES=mail=http://secretary-mail:8080,tapo=http://secretary-tapo:8080
SECRETARY_MODULE_TOKENS=mail=<무작위 값>,tapo=<무작위 값>
```

- 모듈 이름: 영문 소문자, 숫자, 하이픈 2~24자. `system`, `approvals`, `modules`, `events`는 허브가 쓰므로 못 쓴다.
- 토큰은 모듈마다 따로 만든다 (`openssl rand -hex 32`). 모듈 하나가 털려도 다른 모듈을 부를 수 없다.

## 2. 인증

- 허브 → 모듈: `Authorization: Bearer <그 모듈의 토큰>`
- 모듈 → 허브 (이벤트): 같은 토큰. 허브는 토큰으로 어느 모듈인지 알아낸다.
- 모듈은 토큰이 틀리면 401을 돌려준다. 모듈 포트는 바깥에 열지 않는다(내부 Docker 네트워크만).

## 3. 모듈이 제공하는 API

### `GET /manifest`
모듈 소개서. 허브가 1분마다 다시 읽는다.

```json
{
  "name": "mail",
  "title": "메일",
  "actions": {
    "send": {
      "title": "메일 보내기",
      "approval": true,
      "roles": null,
      "params": { "type": "object", "required": ["to", "subject", "body"] }
    }
  }
}
```

| 필드 | 뜻 |
|---|---|
| `name` | 허브에 등록한 이름과 같아야 한다 |
| `actions.<이름>` | 할 수 있는 일. 필요한 scope는 자동으로 `<모듈>:<액션>` (예: `mail:send`) |
| `approval` | `true`면 바로 실행하지 않고 승인 대기열에 올린다 |
| `roles` | 이 액션을 쓸 수 있는 역할 목록. `null`이면 모든 역할 |
| `params` | 입력 형식 설명(JSON Schema). 클라이언트 화면용. 실제 검사는 모듈이 한다 |

### `POST /actions/{action}/validate`
실행하기 전에 입력을 검사하고, 승인 화면에 보여 줄 한 줄 요약을 돌려준다.

요청:
```json
{
  "params": { "to": ["grandma@family.example"], "subject": "퇴근", "body": "지금 출발해요" },
  "caller": { "user_id": "dad", "name": "아빠", "role": "admin", "email": "dad@example.com" }
}
```
응답: `200 {"summary": "메일 → grandma@family.example : 퇴근"}` 또는 `422 {"message": "허용되지 않은 수신자가 있습니다."}`

### `POST /actions/{action}`
실제로 실행한다. 요청 본문은 validate와 같다. 승인이 필요한 액션은 승인된 뒤에 불린다.
모듈은 실행 직전에 입력을 한 번 더 검사해야 한다(요청과 승인 사이에 설정이 바뀌었을 수 있다).

응답: `200 {"result": "발송 완료"}` 또는 `422 {"message": "..."}`. 그 밖의 상태 코드는 허브가 "실행 실패"로 처리한다.

### `GET /healthz`
`200 {"ok": true}`. 인증 없음. Docker healthcheck용.

## 4. 이벤트 콜백 (모듈 → 허브)

모듈이 먼저 알려야 할 일(카카오 알림이 왔다, 대화 요약이 끝났다)은 허브에 이벤트로 보낸다.

```
POST http://secretary:8080/internal/events
Authorization: Bearer <그 모듈의 토큰>

{ "type": "message.received", "user": "dad", "title": "엄마: 저녁 뭐 먹어?" }
```

- `user`가 `null`이면 가족 모두에게 보이는 이벤트.
- `title`은 200자까지. 본문 전체 같은 민감한 내용은 넣지 않는다.
- 클라이언트는 `GET /v1/events`로 가져간다. 허브는 7일이 지난 이벤트를 지운다.
- `/internal/*`은 Cloudflare를 거쳐 온 요청이면 404를 돌려준다. 내부 네트워크에서만 쓸 수 있다.

## 5. 새 모듈 만들기 체크리스트

모듈은 저장소 하나에 하나씩 둔다. 허브 저장소는 고치지 않는다. [Mail](https://github.com/HomeSecretary/Mail) 저장소가 본보기다.

1. 위 4개 API(`/manifest`, `/actions/{a}/validate`, `/actions/{a}`, `/healthz`)를 만든다.
2. 외부 서비스 키는 그 모듈 컨테이너에만 넣는다.
3. 그 저장소의 `docker-compose.yml`에서 `secretary_modules` 네트워크(인터넷이 필요하면 `secretary_egress`도)에 `external: true`로 붙는다. `secretary_edge`에는 붙지 않는다.
   컨테이너 이름은 `secretary-<이름>`, compose 프로젝트 이름은 다른 저장소와 겹치지 않게 한다.
4. 모듈의 `.env`에 `MODULE_TOKEN_<이름>`을 넣고, 허브의 `.env`에서 `SECRETARY_MODULES`, `SECRETARY_MODULE_TOKENS`에 한 줄씩 넣는다 (토큰은 같은 값).
5. 기기에 권한을 준다: `device add dad phone --scope tapo:on` 또는 `--scope tapo:*`
