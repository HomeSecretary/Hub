import pytest

from app.auth.registry import Registry, RegistryError
from app.auth.scopes import ADMIN, CHILD, MEMBER, PRESETS


@pytest.fixture
def reg(tmp_path):
    return Registry(tmp_path / "registry.json", max_users=4)


def test_first_user_must_be_admin(reg):
    with pytest.raises(RegistryError):
        reg.add_user("mom", "엄마", MEMBER)
    reg.add_user("dad", "아빠", ADMIN)
    reg.add_user("mom", "엄마", MEMBER)


def test_max_users(reg):
    reg.add_user("dad", "아빠", ADMIN)
    reg.add_user("mom", "엄마", MEMBER)
    reg.add_user("kid1", "첫째", CHILD)
    reg.add_user("kid2", "둘째", CHILD)
    with pytest.raises(RegistryError, match="최대 4명"):
        reg.add_user("guest", "손님", MEMBER)
    # 한 명을 비활성화하면 자리가 생긴다
    reg.disable_user("kid2")
    reg.add_user("kid3", "셋째", CHILD)


def test_cannot_disable_last_admin(reg):
    reg.add_user("dad", "아빠", ADMIN)
    with pytest.raises(RegistryError):
        reg.disable_user("dad")


def test_disable_user_revokes_devices(reg):
    reg.add_user("dad", "아빠", ADMIN)
    reg.add_user("kid", "첫째", CHILD)
    phone = reg.add_device("kid", "phone", PRESETS["phone"])
    assert reg.disable_user("kid") == 1
    assert reg.device(phone.access_key).revoked


def test_names_must_be_ascii(reg):
    with pytest.raises(RegistryError):
        reg.add_user("아빠", "아빠", ADMIN)
    reg.add_user("dad", "아빠", ADMIN)
    with pytest.raises(RegistryError):
        reg.add_device("dad", "내폰", PRESETS["phone"])


def test_registry_file_is_private(reg):
    reg.add_user("dad", "아빠", ADMIN)
    assert oct(reg.path.stat().st_mode & 0o777) == "0o600"


def test_scope_shapes(reg):
    reg.add_user("dad", "아빠", ADMIN)
    reg.add_device("dad", "tablet", {"system:read", "mail:send", "tapo:*", "modules:*"})
    with pytest.raises(RegistryError, match="scope"):
        reg.add_device("dad", "bad", {"mail"})
    with pytest.raises(RegistryError, match="scope"):
        reg.add_device("dad", "bad", {"Mail:Send"})


def test_wildcard_scopes():
    from app.auth.scopes import allows, effective_scopes

    assert allows({"mail:*"}, "mail:send")
    assert not allows({"mail:*"}, "tapo:on")
    assert allows({"modules:*"}, "tapo:on")
    assert not allows({"modules:*"}, "approvals:decide")  # 모듈 와일드카드는 허브 권한을 주지 않는다
    assert effective_scopes({"approvals:decide", "mail:send", "modules:*"}, CHILD) == {"mail:send", "modules:*"}
    assert effective_scopes({"system:read"}, "nobody") == set()


def test_delete_fresh_user_frees_id(reg):
    reg.add_user("data", "owner", ADMIN)  # 오타로 만든 관리자
    reg.delete_user("data", has_history=False)
    assert reg.user("data") is None
    reg.add_user("dad", "아빠", ADMIN)
    reg.add_user("data", "다시 쓰는 ID", MEMBER)


def test_cannot_delete_user_with_devices(reg):
    reg.add_user("dad", "아빠", ADMIN)
    reg.add_user("kid", "첫째", CHILD)
    phone = reg.add_device("kid", "phone", PRESETS["phone"])
    reg.revoke(phone.access_key)  # 폐기된 기기도 기록이므로 지울 수 없다
    with pytest.raises(RegistryError, match="disable"):
        reg.delete_user("kid", has_history=False)


def test_cannot_delete_user_with_approval_history(reg):
    reg.add_user("dad", "아빠", ADMIN)
    reg.add_user("mom", "엄마", MEMBER)
    with pytest.raises(RegistryError, match="승인 기록"):
        reg.delete_user("mom", has_history=True)


def test_cannot_delete_last_admin_while_others_exist(reg):
    reg.add_user("dad", "아빠", ADMIN)
    reg.add_user("mom", "엄마", MEMBER)
    with pytest.raises(RegistryError, match="마지막 관리자"):
        reg.delete_user("dad", has_history=False)
    reg.delete_user("mom", has_history=False)
    reg.delete_user("dad", has_history=False)  # 혼자 남은 관리자는 지울 수 있다 (처음부터 다시)
    assert reg.users() == []


def test_approval_history(tmp_path):
    from app.approvals import ApprovalQueue
    from app.db import Database

    q = ApprovalQueue(Database(tmp_path / "s.db"), ttl_seconds=300)
    assert not q.has_history("mom")
    q.create("mail.send", {"module": "mail", "action": "send", "params": {}}, "요약", "mom", MEMBER, "mom-phone-1234")
    assert q.has_history("mom")


def test_cli_user_delete(tmp_path):
    import os
    import subprocess
    import sys

    env = {**os.environ, "SECRETARY_DATA_DIR": str(tmp_path)}

    def cli(*args):
        return subprocess.run([sys.executable, "-m", "app.cli", *args], env=env, capture_output=True, text=True)

    assert cli("user", "add", "data", "owner", "--role", "admin").returncode == 0
    r = cli("user", "delete", "data")
    assert r.returncode == 0, r.stderr
    assert "삭제했습니다" in r.stdout
    r = cli("user", "delete", "data")
    assert r.returncode == 2 and "없습니다" in r.stderr
    assert '"event": "user.delete"' in (tmp_path / "audit.log").read_text()
