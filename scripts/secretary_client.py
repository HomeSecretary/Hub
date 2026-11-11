#!/usr/bin/env python3
"""Secretary 호출 예제 클라이언트. 표준 라이브러리만 쓴다.

다른 클라이언트(Flutter, G2, Zepp)도 이 파일의 sign_headers()와 같은 방식으로 서명하면 된다.

환경 변수:
  SECRETARY_URL          https://api.example.house
  CF_CLIENT_ID           Cloudflare Access 서비스 토큰 ID
  CF_CLIENT_SECRET       Cloudflare Access 서비스 토큰 Secret
  SECRETARY_ACCESS_KEY   Secretary 기기 키
  SECRETARY_SECRET       Secretary 기기 Secret

사용 예:
  python3 secretary_client.py GET /v1/me
  python3 secretary_client.py GET /v1/modules
  python3 secretary_client.py POST /v1/commands '{"module":"mail","action":"send","params":{"to":["me@example.com"],"subject":"테스트","body":"안녕"}}'
  python3 secretary_client.py GET "/v1/approvals?status=pending"
  python3 secretary_client.py POST /v1/approvals/<id>/approve
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "secretary-client/1.0"


def sign_headers(access_key: str, secret: str, method: str, path: str, query: str, body: bytes) -> dict:
    """요청 하나에 붙일 서명 헤더 4개를 만든다. 서버의 app/auth/signing.py와 똑같은 방식.

    다른 언어로 클라이언트를 만들 때 이 함수만 옮겨 적으면 된다.
    """
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    target = f"{path}?{query}" if query else path
    canonical = "\n".join([method.upper(), target, timestamp, nonce, hashlib.sha256(body).hexdigest()])
    digest = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest()
    return {
        "X-Access-Key": access_key,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": base64.urlsafe_b64encode(digest).decode().rstrip("="),
    }


def main() -> int:
    """명령줄 인자로 받은 요청을 보내고, 상태 코드와 응답 JSON을 보기 좋게 출력한다.

    Cloudflare Access 서비스 토큰 헤더 + Secretary 서명 헤더를 둘 다 붙인다. 400 이상이면 종료 코드 1.
    """
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    method, target = sys.argv[1].upper(), sys.argv[2]
    body = sys.argv[3].encode() if len(sys.argv) > 3 else b""
    parsed = urllib.parse.urlsplit(target)

    env = os.environ
    headers = {
        # urllib 기본 User-Agent(Python-urllib/3.x)는 Cloudflare Browser Integrity Check에 막힌다 (error code: 1010)
        "User-Agent": USER_AGENT,
        "CF-Access-Client-Id": env["CF_CLIENT_ID"],
        "CF-Access-Client-Secret": env["CF_CLIENT_SECRET"],
        **sign_headers(env["SECRETARY_ACCESS_KEY"], env["SECRETARY_SECRET"], method, parsed.path, parsed.query, body),
    }
    if body:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(env["SECRETARY_URL"].rstrip("/") + target, data=body or None, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            status, payload = res.status, res.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    print(status)
    try:
        print(json.dumps(json.loads(payload), ensure_ascii=False, indent=2))
    except ValueError:
        print(payload.decode(errors="replace")[:500])
    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
