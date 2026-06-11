#!/usr/bin/env python3
"""Debug AICP memory collection resources and SDK read/write behavior.

Examples:
  python ksadk-python/scripts/debug_aicp_memory.py service-status
  python ksadk-python/scripts/debug_aicp_memory.py list
  python ksadk-python/scripts/debug_aicp_memory.py get --memory-id mem-xxx
  python ksadk-python/scripts/debug_aicp_memory.py create --name demo --description "debug"
  python ksadk-python/scripts/debug_aicp_memory.py write \
    --memory-id mem-xxx --user-id debug-user --text ping
  python ksadk-python/scripts/debug_aicp_memory.py query \
    --memory-id mem-xxx --user-id debug-user --query ping
  python ksadk-python/scripts/debug_aicp_memory.py list-sessions \
    --memory-id mem-xxx --user-id debug-user
  python ksadk-python/scripts/debug_aicp_memory.py session-memories \
    --memory-id mem-xxx --session-id sess-xxx
  python ksadk-python/scripts/debug_aicp_memory.py metrics --memory-id mem-xxx
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


def load_simple_dotenv(env_file: str | None = None) -> None:
    """Load .env files without requiring python-dotenv."""
    candidates = []
    if env_file:
        candidates.append(Path(env_file).expanduser())
    candidates.extend(
        [
            Path.cwd() / ".env",
            Path.cwd().parent / ".env",
            Path(__file__).resolve().parents[1] / ".env",
            Path(__file__).resolve().parents[2] / ".env",
            Path(__file__).resolve().parents[3] / ".env",
        ]
    )

    for env_path in candidates:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            os.environ.setdefault(key, value)


def build_client():
    from ksyun.client.aicp.v20251114 import client
    from ksyun.common import credential
    from ksyun.common.profile.client_profile import ClientProfile
    from ksyun.common.profile.http_profile import HttpProfile

    access_key = os.getenv("KSADK_LTM_ACCESS_KEY") or os.getenv("KSYUN_ACCESS_KEY")
    secret_key = os.getenv("KSADK_LTM_SECRET_KEY") or os.getenv("KSYUN_SECRET_KEY")
    region = os.getenv("KSADK_LTM_REGION", "cn-beijing-6")
    endpoint = os.getenv("KSADK_LTM_ENDPOINT", "aicp.api.ksyun.com")
    scheme = os.getenv("KSADK_LTM_SCHEME", "https")

    if not access_key or not secret_key:
        raise SystemExit(
            "Missing AK/SK. Set KSADK_LTM_ACCESS_KEY/SECRET_KEY "
            "or KSYUN_ACCESS_KEY/SECRET_KEY."
        )

    cred = credential.Credential(access_key, secret_key)
    http = HttpProfile()
    http.endpoint = endpoint
    http.reqMethod = "POST"
    http.reqTimeout = 60
    http.scheme = scheme

    profile = ClientProfile()
    profile.httpProfile = http

    cli = client.AicpClient(cred, region, profile=profile)
    return cli, {
        "region": region,
        "endpoint": endpoint,
        "scheme": scheme,
        "access_key_tail": access_key[-6:],
    }


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def safe_call(label: str, func, request=None):
    try:
        body = func(request) if request is not None else func()
        parsed = json.loads(body) if isinstance(body, str) else body
        return {"ok": True, "action": label, "response": parsed}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "action": label,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def cmd_service_status(cli):
    from ksyun.client.aicp.v20251114 import models

    req = models.GetMemoryBaseServiceRequest()
    return safe_call("GetMemoryBaseService", cli.GetMemoryBaseService, req)


def cmd_list(cli, args):
    from ksyun.client.aicp.v20251114 import models

    req = models.ListMemoryCollectionsRequest()
    if args.name_keyword:
        req.NameKeyword = args.name_keyword
    if args.name:
        req.Name = args.name
    if args.memory_id:
        req.MemoryCollectionId = args.memory_id
    if args.status:
        req.Status = args.status
    req.Marker = args.marker
    req.MaxResults = args.max_results
    return safe_call("ListMemoryCollections", cli.ListMemoryCollections, req)


def cmd_get(cli, args):
    from ksyun.client.aicp.v20251114 import models

    req = models.GetMemoryCollectionRequest()
    req.MemoryCollectionId = args.memory_id
    return safe_call("GetMemoryCollection", cli.GetMemoryCollection, req)


def cmd_create(cli, args):
    from ksyun.client.aicp.v20251114 import models

    req = models.CreateMemoryCollectionRequest()
    req.Name = args.name
    if args.description:
        req.Description = args.description
    return safe_call("CreateMemoryCollection", cli.CreateMemoryCollection, req)


def raw_sdk_call(cli, action: str, params: dict[str, Any]) -> dict[str, Any]:
    safe_params = copy.deepcopy(params)
    if "Accesskey" in safe_params and isinstance(safe_params["Accesskey"], str):
        ak = safe_params["Accesskey"]
        safe_params["Accesskey"] = f"{ak[:4]}...{ak[-4:]}"
    try:
        body = cli.call(action, params, options={"IsPostJson": True})
        parsed = json.loads(body) if isinstance(body, str) else body
        return {"ok": True, "action": action, "params": safe_params, "response": parsed}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "action": action,
            "params": safe_params,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def cmd_write(cli, args):
    params = {
        "MemoryCollectionId": args.memory_id,
        "AgentUserId": args.user_id,
        "SceneId": args.scene_id,
        "Data": {
            "Conversation": [
                {
                    "Role": args.role,
                    "CreatedAt": int(time.time() * 1000),
                    "MessageId": str(uuid.uuid4()),
                    "Content": [{"Type": "input_text", "Text": args.text}],
                }
            ]
        },
    }
    if args.agent_id:
        params["AgentId"] = args.agent_id
    if args.session_id:
        params["SessionId"] = args.session_id
    return raw_sdk_call(cli, "CreateMemorySdk", params)


def cmd_query(cli, args):
    params = {
        "MemoryCollectionId": args.memory_id,
        "AgentUserId": args.user_id,
        "SceneId": args.scene_id,
        "Query": args.query,
        "Limit": args.limit,
    }
    if args.mode:
        params["Mode"] = args.mode
    return raw_sdk_call(cli, "QueryMemorySdk", params)


def cmd_list_sessions(cli, args):
    params = {
        "MemoryCollectionId": args.memory_id,
        "AgentUserId": args.user_id,
        "Page": args.page,
        "PageSize": args.page_size,
    }
    if args.query:
        params["Query"] = args.query
    if args.created_after:
        params["CreatedAfter"] = args.created_after
    if args.created_before:
        params["CreatedBefore"] = args.created_before
    return raw_sdk_call(cli, "ListSessions", params)


def cmd_session_memories(cli, args):
    params = {
        "MemoryCollectionId": args.memory_id,
        "SessionId": args.session_id,
    }
    return raw_sdk_call(cli, "QuerySessionMemories", params)


def cmd_metrics(cli, args):
    end_time = args.end_time or int(time.time())
    start_time = args.start_time or end_time - args.last_seconds
    params = {
        "MemoryCollectionId": args.memory_id,
        "StartTime": start_time,
        "EndTime": end_time,
    }
    return raw_sdk_call(cli, "QueryMemoryCollectionMetrics", params)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        help="Optional path to a .env file. Values are loaded before reading process env.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("service-status", help="Get memory base service status")

    list_parser = subparsers.add_parser("list", help="List memory collections")
    list_parser.add_argument("--name-keyword")
    list_parser.add_argument("--name")
    list_parser.add_argument("--memory-id")
    list_parser.add_argument("--status")
    list_parser.add_argument("--marker", type=int, default=1)
    list_parser.add_argument("--max-results", type=int, default=20)

    get_parser = subparsers.add_parser("get", help="Get a memory collection by id")
    get_parser.add_argument("--memory-id", required=True)

    create_parser = subparsers.add_parser("create", help="Create a memory collection")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--description", default="")

    write_parser = subparsers.add_parser("write", help="Write memory into a memory collection")
    write_parser.add_argument("--memory-id", "--namespace", dest="memory_id", required=True)
    write_parser.add_argument("--user-id", default="debug-user")
    write_parser.add_argument("--text", required=True)
    write_parser.add_argument("--role", default="user")
    write_parser.add_argument("--agent-id", default="")
    write_parser.add_argument("--session-id", default="")
    write_parser.add_argument("--scene-id", default="_sys_general")

    query_parser = subparsers.add_parser("query", help="Query memory from a memory collection")
    query_parser.add_argument("--memory-id", "--namespace", dest="memory_id", required=True)
    query_parser.add_argument("--user-id", default="debug-user")
    query_parser.add_argument("--query", required=True)
    query_parser.add_argument("--limit", type=int, default=5)
    query_parser.add_argument("--scene-id", default="_sys_general")
    query_parser.add_argument("--mode", default="")

    sessions_parser = subparsers.add_parser(
        "list-sessions", help="List raw memory sessions for a user"
    )
    sessions_parser.add_argument("--memory-id", "--namespace", dest="memory_id", required=True)
    sessions_parser.add_argument("--user-id", default="debug-user")
    sessions_parser.add_argument("--query", default="")
    sessions_parser.add_argument("--page", type=int, default=1)
    sessions_parser.add_argument("--page-size", type=int, default=20)
    sessions_parser.add_argument("--created-after", type=int, default=0)
    sessions_parser.add_argument("--created-before", type=int, default=0)

    session_memories_parser = subparsers.add_parser(
        "session-memories", help="Query extracted memories for one raw session"
    )
    session_memories_parser.add_argument("--memory-id", "--namespace", dest="memory_id", required=True)
    session_memories_parser.add_argument("--session-id", required=True)

    metrics_parser = subparsers.add_parser(
        "metrics", help="Query memory collection action metrics"
    )
    metrics_parser.add_argument("--memory-id", "--namespace", dest="memory_id", required=True)
    metrics_parser.add_argument("--start-time", type=int, default=0)
    metrics_parser.add_argument("--end-time", type=int, default=0)
    metrics_parser.add_argument("--last-seconds", type=int, default=3600)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    load_simple_dotenv(args.env_file)
    cli, config = build_client()

    print_json({"config": config, "command": args.command})

    if args.command == "service-status":
        result = cmd_service_status(cli)
    elif args.command == "list":
        result = cmd_list(cli, args)
    elif args.command == "get":
        result = cmd_get(cli, args)
    elif args.command == "create":
        result = cmd_create(cli, args)
    elif args.command == "write":
        result = cmd_write(cli, args)
    elif args.command == "query":
        result = cmd_query(cli, args)
    elif args.command == "list-sessions":
        result = cmd_list_sessions(cli, args)
    elif args.command == "session-memories":
        result = cmd_session_memories(cli, args)
    elif args.command == "metrics":
        result = cmd_metrics(cli, args)
    else:
        parser.error(f"Unsupported command: {args.command}")
        return 2

    print_json(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
