from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from pydantic import ValidationError

from juli.adapters.scenario_files import load_scenario
from juli.bootstrap import load_registry, tool_contracts
from juli.core.common import DomainError
from juli.core.scenarios import ScenarioRegistry


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="据理：通用合同与场景注册工具（当前不执行 AI 分析）")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="检查一个部署者安装的场景包")
    validate.add_argument("package", type=Path)
    db_parser = commands.add_parser("db", help="显式创建或检查据理独立数据库")
    db_parser.add_argument("operation", choices=("migrate", "check"))
    db_parser.add_argument("--database", type=Path, required=True)
    identity = commands.add_parser("identity", help="本地管理员管理主体与个人访问令牌")
    identity.add_argument("operation", choices=("create", "issue", "revoke", "disable", "enable"))
    identity.add_argument("--database", type=Path, required=True)
    identity.add_argument("--name")
    identity.add_argument("--principal-id")
    identity.add_argument("--token-id")
    identity.add_argument("--ttl-days", type=int, default=30)
    for command in ("scenarios", "demo", "model-demo", "runtime-demo", "serve"):
        sub = commands.add_parser(command)
        sub.add_argument("--directory", type=Path, default=Path("scenarios"))
        if command == "serve":
            sub.add_argument("--port", type=int, default=8790)
            sub.add_argument("--database", type=Path)
            sub.add_argument("--demo-model", action="store_true", help="启用明确标记的离线演示模型，仅支持 minimal 场景")
    args = parser.parse_args()
    try:
        if args.command == "db":
            from juli.adapters.sqlite_migrations import LATEST_VERSION, migrate, transaction
            if args.operation == "migrate":
                output = migrate(args.database)
            else:
                with transaction(args.database):
                    output = {"database_ready": True, "version": LATEST_VERSION}
        elif args.command == "identity":
            from juli.adapters.sqlite_store import SQLiteStore
            store = SQLiteStore(args.database)
            if args.operation in {"create", "issue"}:
                if args.operation == "create":
                    if not args.name:
                        raise ValueError("--name is required for identity create")
                    issued = store.provision_principal(args.name, ttl_days=args.ttl_days)
                else:
                    if not args.principal_id:
                        raise ValueError("--principal-id is required for identity issue")
                    issued = store.issue_token(args.principal_id, ttl_days=args.ttl_days)
                # Only explicit local administration prints the newly issued secret once.
                output = {"principal_id": issued.principal_id, "token_id": issued.token_id,
                          "expires_at": issued.expires_at, "token": issued.token}
            elif args.operation == "revoke":
                if not args.token_id:
                    raise ValueError("--token-id is required for identity revoke")
                store.revoke_token(args.token_id)
                output = {"revoked": True}
            else:
                if not args.principal_id:
                    raise ValueError("--principal-id is required")
                store.set_principal_enabled(args.principal_id, args.operation == "enable")
                output = {"enabled": args.operation == "enable"}
        elif args.command == "validate":
            package = load_scenario(args.package)
            registry = ScenarioRegistry(tool_contracts())
            registry.register(package)
            output = {"valid":True,"scenario":package.manifest.id,"version":package.manifest.version,"fingerprint":package.fingerprint}
        else:
            registry = load_registry(args.directory)
            if args.command == "scenarios":
                output = {"items":[{"id":p.manifest.id,"version":p.manifest.version,"name":p.manifest.name,"fingerprint":p.fingerprint} for p in registry.list()]}
            elif args.command == "demo":
                from juli.demo import contract_demo
                output = contract_demo(registry)
            elif args.command == "model-demo":
                from juli.model_demo import model_demo
                output = model_demo(registry)
            elif args.command == "runtime-demo":
                from juli.runtime_demo import runtime_demo
                output = runtime_demo(registry)
            else:
                from juli.api import create_app
                import uvicorn
                if args.demo_model and not args.database:
                    raise ValueError('--demo-model requires --database')
                from juli.adapters.workbench_demo import demo_config
                runtime = demo_config() if args.demo_model else None
                app = create_app(registry, database=args.database, runtime=runtime) if args.database else create_app(registry, api_token=os.environ.get("JULI_API_TOKEN", ""))
                uvicorn.run(app, host="127.0.0.1", port=args.port)
                return 0
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (DomainError, ValidationError, ValueError, OSError) as exc:
        # Avoid disclosing raw environment values or file contents on the API.
        print(json.dumps({"error":getattr(exc,"code","invalid_configuration"),"message":str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
