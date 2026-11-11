"""사용자·기기 관리 CLI. NAS 안에서만 실행한다(외부 API로는 키를 발급할 수 없다).

  # 사용자 (최대 SECRETARY_MAX_USERS명, 기본 4명. 첫 사용자는 admin)
  docker exec -it secretary python -m app.cli user add dad "아빠" --role admin --email dad@example.com
  docker exec -it secretary python -m app.cli user add kid1 "첫째" --role child
  docker exec -it secretary python -m app.cli user list
  docker exec -it secretary python -m app.cli user disable kid1        # 그 사람의 기기 키도 전부 폐기
  docker exec -it secretary python -m app.cli user delete typo-id      # 기기·승인 기록이 없는 사용자만 완전 삭제

  # 기기
  docker exec -it secretary python -m app.cli device add dad phone --preset phone
  docker exec -it secretary python -m app.cli device list
  docker exec -it secretary python -m app.cli device revoke <access_key>
  docker exec -it secretary python -m app.cli device revoke-all       # 비상 정지

  # 모듈
  docker exec -it secretary python -m app.cli module list             # 연결 상태와 액션(= scope) 목록
"""
import argparse
import sys
from datetime import datetime

import httpx

from .audit import AuditLog
from .auth.registry import Registry, RegistryError
from .catalog import Manifest
from .auth.scopes import HUB_SCOPES, PRESETS, ROLES, effective_scopes
from .config import get_settings
from .db import Database
from .approvals import ApprovalQueue


def _fmt(ts: int | None) -> str:
    """유닉스 시각(초)을 '2026-11-04 21:30' 같은 글자로 바꾼다. 값이 없으면 '-'."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def build_parser() -> argparse.ArgumentParser:
    """명령어 구조를 정의한다. user/device 아래에 add, list ... 같은 하위 명령이 붙는다.

    argparse가 잘못된 입력을 걸러 주고, -h 도움말도 자동으로 만든다.
    """
    parser = argparse.ArgumentParser(prog="secretary")
    sub = parser.add_subparsers(dest="cmd", required=True)

    usr = sub.add_parser("user", help="사용자 관리").add_subparsers(dest="action", required=True)
    ua = usr.add_parser("add", help="사용자 추가")
    ua.add_argument("user_id", help="영문 소문자 ID. 예: dad, mom, kid1")
    ua.add_argument("name", help="표시 이름. 메일 보낸 사람 이름으로도 쓰인다")
    ua.add_argument("--role", choices=ROLES, required=True)
    ua.add_argument("--email", help="본인 이메일. 모듈에 함께 전달된다 (메일 모듈은 Reply-To와 본인에게 보내기 허용에 쓴다)")
    usr.add_parser("list", help="사용자 목록")
    ud = usr.add_parser("disable", help="사용자 비활성화 (기기 키도 전부 폐기)")
    ud.add_argument("user_id")
    udel = usr.add_parser("delete", help="사용자 완전 삭제 (기기·승인 기록이 없을 때만)")
    udel.add_argument("user_id")

    dev = sub.add_parser("device", help="기기 관리").add_subparsers(dest="action", required=True)
    da = dev.add_parser("add", help="기기 키 발급")
    da.add_argument("user_id")
    da.add_argument("name", help="영문 소문자 기기 이름. 예: phone, watch, glasses")
    da.add_argument("--preset", choices=sorted(PRESETS))
    da.add_argument("--scope", action="append", default=[], help=f"직접 지정 (여러 번 가능): {', '.join(sorted(HUB_SCOPES))}, <모듈>:<액션>, <모듈>:*, modules:*")
    dev.add_parser("list", help="기기 목록")
    dr = dev.add_parser("revoke", help="기기 키 폐기")
    dr.add_argument("access_key")
    dev.add_parser("revoke-all", help="모든 기기 키 폐기 (비상 정지)")

    mod = sub.add_parser("module", help="모듈 확인").add_subparsers(dest="action", required=True)
    mod.add_parser("list", help="등록된 모듈의 연결 상태와 액션 목록")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 시작점. 명령을 해석해 _user, _device, _module로 넘긴다.

    돌려주는 값은 종료 코드: 0 성공, 1 대상 없음, 2 입력 오류(규칙 위반).
    """
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.cmd == "module":
        return _module(settings)
    reg = Registry(settings.registry_path, settings.max_users)
    audit = AuditLog(settings.audit_path)

    try:
        if args.cmd == "user":
            return _user(args, reg, audit)
        return _device(args, reg, audit)
    except RegistryError as e:
        print(e, file=sys.stderr)
        return 2


def _user(args, reg: Registry, audit: AuditLog) -> int:
    """user add / list / disable / delete 를 처리한다. 바꾼 내용은 감사 로그에 via=cli로 남긴다."""
    if args.action == "add":
        user = reg.add_user(args.user_id, args.name, args.role, args.email)
        audit.write("user.add", user=user.id, via="cli", role=user.role)
        print(f"사용자를 추가했습니다: {user.id} ({user.name}, {user.role})")
        return 0
    if args.action == "list":
        users = reg.users()
        active = sum(1 for u in users if not u.disabled)
        print(f"활성 사용자 {active}/{reg.max_users}명")
        for u in users:
            state = "비활성" if u.disabled else "활성"
            print(f"  {u.id:<12} {u.name:<10} {u.role:<7} {state:<4} {u.email or '-'}")
        return 0
    if args.action == "disable":
        count = reg.disable_user(args.user_id)
        audit.write("user.disable", user=args.user_id, via="cli", revoked_devices=count)
        print(f"비활성화했습니다. 기기 키 {count}개를 폐기했습니다.")
        return 0
    if args.action == "delete":
        settings = get_settings()
        db = Database(settings.db_path)
        try:
            history = ApprovalQueue(db, settings.approval_ttl_seconds).has_history(args.user_id)
        finally:
            db.close()
        user = reg.delete_user(args.user_id, has_history=history)
        audit.write("user.delete", user=user.id, via="cli", role=user.role)
        print(f"삭제했습니다: {user.id} ({user.name}, {user.role})")
        return 0
    return 1


def _device(args, reg: Registry, audit: AuditLog) -> int:
    """device add / list / revoke / revoke-all 을 처리한다.

    add는 Secret을 이때 한 번만 화면에 보여 준다. 역할 상한 때문에 빠지는 scope가 있으면 같이 알려 준다.
    """
    if args.action == "add":
        scopes = set(PRESETS.get(args.preset, set())) | set(args.scope)
        if not scopes:
            print("--preset 또는 --scope 중 하나는 지정해야 합니다.", file=sys.stderr)
            return 2
        device = reg.add_device(args.user_id, args.name, scopes)
        user = reg.user(args.user_id)
        effective = effective_scopes(device.scopes, user.role)
        audit.write("device.add", user=user.id, device=device.access_key, via="cli", scopes=device.scopes)
        print("기기 키를 발급했습니다. Secret은 지금 한 번만 보여 줍니다.\n")
        print(f"  사용자     : {user.id} ({user.name}, {user.role})")
        print(f"  Access Key : {device.access_key}")
        print(f"  Secret     : {device.secret}")
        print(f"  Scopes     : {', '.join(sorted(effective))}")
        dropped = set(device.scopes) - effective
        if dropped:
            print(f"  (역할 {user.role} 상한 때문에 실제로는 쓸 수 없는 scope: {', '.join(sorted(dropped))})")
        return 0
    if args.action == "list":
        for d in reg.devices():
            state = "폐기됨" if d.revoked else "사용 중"
            print(f"{d.access_key:<32} {d.user:<10} {state:<5} 발급 {_fmt(d.created_at)}  {', '.join(d.scopes)}")
        return 0
    if args.action == "revoke":
        ok = reg.revoke(args.access_key)
        if ok:
            audit.write("device.revoke", device=args.access_key, via="cli")
        print("폐기했습니다." if ok else "해당 키가 없거나 이미 폐기됐습니다.")
        return 0 if ok else 1
    if args.action == "revoke-all":
        count = reg.revoke_all()
        audit.write("device.revoke_all", via="cli", count=count)
        print(f"{count}개 키를 폐기했습니다.")
        return 0
    return 1


def _module(settings) -> int:
    """module list 를 처리한다. 모듈마다 manifest를 받아 와서 액션과 필요한 scope를 보여 준다.

    기기에 어떤 --scope 를 줘야 할지 모를 때 이 목록을 보면 된다.
    """
    endpoints = settings.module_endpoints
    if not endpoints:
        print("등록된 모듈이 없습니다. SECRETARY_MODULES 를 확인하세요.")
        return 0
    for name, (url, token) in endpoints.items():
        try:
            res = httpx.get(f"{url}/manifest", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            res.raise_for_status()
            manifest = Manifest.model_validate(res.json())
        except Exception as e:  # 어떤 이유든 "연결 안 됨"으로 보여 주고 다음 모듈로
            print(f"{name:<12} 연결 안 됨  {url}  ({type(e).__name__})")
            continue
        print(f"{name:<12} 정상        {url}  ({manifest.title})")
        for action, spec in manifest.actions.items():
            notes = (["승인 필요"] if spec.approval else []) + ([f"역할: {', '.join(spec.roles)}"] if spec.roles else [])
            print(f"    {name}:{action:<20} {spec.title}{'  (' + ', '.join(notes) + ')' if notes else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
