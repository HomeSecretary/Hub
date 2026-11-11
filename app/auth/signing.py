"""HMAC 요청 서명. 서버와 클라이언트 예제가 같은 함수를 쓴다.

서명할 문자열 = METHOD \\n PATH[?QUERY] \\n TIMESTAMP \\n NONCE \\n SHA256(body) 16진수
서명 = base64url(HMAC-SHA256(secret, 서명할 문자열))
"""
import base64
import hashlib
import hmac
import secrets
import time

HEADER_KEY = "X-Access-Key"
HEADER_TIMESTAMP = "X-Timestamp"
HEADER_NONCE = "X-Nonce"
HEADER_SIGNATURE = "X-Signature"


def canonical_string(method: str, path: str, query: str, timestamp: str, nonce: str, body: bytes) -> str:
    """서명할 문자열을 만든다. 서버와 클라이언트가 글자 하나까지 똑같이 만들어야 서명이 맞는다.

    예) GET /v1/me, 본문 없음 →
        GET
        /v1/me
        1762300000
        3f9c...(nonce)
        e3b0c442...(빈 본문의 SHA-256)
    본문은 통째로 넣지 않고 해시만 넣는다. 본문이 한 글자라도 바뀌면 해시가 달라진다.
    """
    target = f"{path}?{query}" if query else path
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([method.upper(), target, timestamp, nonce, body_hash])


def sign(secret: str, canonical: str) -> str:
    """서명할 문자열을 Secret으로 HMAC-SHA256 서명해 base64url 문자열로 돌려준다.

    base64url은 +, / 대신 -, _ 를 써서 HTTP 헤더에 그대로 넣을 수 있고, 끝의 = 채움 글자는 뗀다.
    """
    digest = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def build_headers(access_key: str, secret: str, method: str, path: str, query: str = "", body: bytes = b"") -> dict:
    """요청 하나에 붙일 인증 헤더 4개를 만든다 (테스트와 예제 클라이언트용).

    타임스탬프는 지금 시각(초), Nonce는 매번 새 무작위 값이라 같은 요청도 매번 서명이 달라진다.
    """
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    canonical = canonical_string(method, path, query, timestamp, nonce, body)
    return {
        HEADER_KEY: access_key,
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_SIGNATURE: sign(secret, canonical),
    }
