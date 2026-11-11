"""예제 클라이언트의 서명 방식이 서버와 같은지 확인한다."""
import importlib.util
from pathlib import Path

from app.auth.signing import canonical_string, sign

# 저장소에서는 루트의 scripts/, 테스트 이미지 안에서는 /app/scripts/ 에 있다. 위로 올라가며 찾는다.
CLIENT = next(p / "scripts" / "secretary_client.py" for p in Path(__file__).resolve().parents
              if (p / "scripts" / "secretary_client.py").exists())
spec = importlib.util.spec_from_file_location("secretary_client", CLIENT)
assert spec is not None
assert spec.loader is not None
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def test_client_signature_matches_server():
    body = b'{"a":1}'
    h = client.sign_headers("phone-1234", "s3cret", "post", "/v1/commands", "x=1", body)
    canonical = canonical_string("POST", "/v1/commands", "x=1", h["X-Timestamp"], h["X-Nonce"], body)
    assert h["X-Signature"] == sign("s3cret", canonical)
