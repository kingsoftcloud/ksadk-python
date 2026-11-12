"""Bounded, read-only PostgreSQL connectivity and ownership diagnostics."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import socket
import ssl
import time
from typing import Any

from .database_access_contract import (
    ACTION,
    ConnectionTarget,
    DatabaseAccessRequest,
)

logger = logging.getLogger(__name__)


class _DatabaseAccessFallbackHandler(logging.StreamHandler):
    """Use stderr only while this diagnostic has no application log handler."""

    def emit(self, record: logging.LogRecord) -> None:
        current = logger
        while current is not None:
            if any(handler is not self for handler in current.handlers):
                # Preserve configured formatters, filters and destinations,
                # including handlers installed after the module was imported.
                return
            if not current.propagate:
                break
            current = current.parent
        super().emit(record)


# `ksadk web` configures Uvicorn's loggers, but leaves business logging unset.
# Scope the default and fallback to this Action; never configure the root logger.
if logger.level == logging.NOTSET:
    logger.setLevel(logging.INFO)
if not any(handler.name == f"{ACTION}.fallback" for handler in logger.handlers):
    _fallback_handler = _DatabaseAccessFallbackHandler()
    _fallback_handler.set_name(f"{ACTION}.fallback")
    _fallback_handler.setLevel(logging.INFO)
    _fallback_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(_fallback_handler)


class TLSNegotiationError(ConnectionError):
    """A required PostgreSQL SSL upgrade was refused by the server."""


IDENTITY_SQL = """
SELECT session_user AS session_user, current_user AS current_user,
       r.rolsuper AS is_superuser, d.datname AS database_name,
       pg_catalog.pg_get_userbyid(d.datdba) AS owner,
       d.datdba = r.oid AS is_owner,
       (r.rolsuper OR pg_catalog.pg_has_role(r.oid, d.datdba, 'USAGE')) AS has_owner_privileges,
       pg_catalog.current_schema() AS default_schema
FROM pg_catalog.pg_database d
JOIN pg_catalog.pg_roles r ON r.rolname = current_user
WHERE d.datname = pg_catalog.current_database()
"""
SCHEMA_SQL = """
SELECT n.nspname AS name, pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
       pg_catalog.has_schema_privilege(current_user, n.oid, 'USAGE') AS can_use,
       pg_catalog.has_schema_privilege(current_user, n.oid, 'CREATE') AS can_create
FROM pg_catalog.pg_namespace n WHERE n.nspname = $1
"""
TABLES_SQL = """
SELECT c.relname AS name, pg_catalog.pg_get_userbyid(c.relowner) AS owner,
       c.relowner = r.oid AS is_owner,
       (r.rolsuper OR pg_catalog.pg_has_role(r.oid, c.relowner, 'USAGE')) AS has_owner_privileges
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_roles r ON r.rolname = current_user
WHERE n.nspname = $1 AND c.relname = ANY($2::text[]) AND c.relkind IN ('r', 'p', 'f')
"""


def empty_permissions(request: DatabaseAccessRequest) -> dict:
    return {
        "Status": "unavailable",
        "SessionUser": None,
        "CurrentUser": None,
        "IsSuperuser": None,
        "Database": {"Name": None, "Owner": None, "IsOwner": None, "HasOwnerPrivileges": None},
        "Schema": {
            "Name": request.Schema,
            "Exists": None,
            "Owner": None,
            "CanUse": None,
            "CanCreate": None,
        },
        "Tables": [
            {
                "Name": name,
                "Schema": request.Schema,
                "Exists": None,
                "Owner": None,
                "IsOwner": None,
                "HasOwnerPrivileges": None,
            }
            for name in request.Tables
        ],
        "Warnings": [],
    }


def classify_error(exc: Exception, stage: str) -> tuple[str, str]:
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate == "28P01" or (sqlstate and sqlstate.startswith("28")):
        return "AUTH_FAILED", "数据库认证失败，请检查用户名、密码或认证配置"
    if sqlstate == "3D000":
        return "DATABASE_NOT_FOUND", "指定的数据库不存在"
    if sqlstate == "53300":
        return "TOO_MANY_CONNECTIONS", "数据库连接数已达上限"
    if sqlstate == "42501":
        return "PERMISSION_DENIED", "当前角色没有数据库连接或基础查询所需的权限"
    if isinstance(exc, socket.gaierror):
        return "DNS_RESOLUTION_FAILED", "无法解析数据库地址"
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError, TLSNegotiationError)):
        return "TLS_FAILED", "数据库 TLS 协商或证书校验失败"
    if isinstance(exc, TimeoutError):
        return (
            ("CONNECT_TIMEOUT", "数据库连接超时，请检查访问规则、路由及服务状态")
            if stage == "connect"
            else ("QUERY_TIMEOUT", "数据库基础查询超时")
        )
    if isinstance(exc, ConnectionRefusedError) or getattr(exc, "errno", None) == errno.ECONNREFUSED:
        return "CONNECTION_REFUSED", "数据库连接被拒绝，请检查端口及服务状态"
    if stage == "query":
        return "QUERY_FAILED", "数据库已连接，但基础查询失败"
    return "CONNECTION_FAILED", "数据库连接失败，请检查连接配置及数据库服务状态"


def _ssl_context(mode: str) -> ssl.SSLContext:
    # Do not let asyncpg load ~/.postgresql client keys or PGSSL* credentials.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = mode == "verify-full"
    context.verify_mode = (
        ssl.CERT_REQUIRED if mode in {"verify-ca", "verify-full"} else ssl.CERT_NONE
    )
    if context.verify_mode == ssl.CERT_REQUIRED:
        context.load_default_certs()
    return context


async def connect_target(target: ConnectionTarget, deadline: float):
    import asyncpg

    async def connect(encrypted: bool):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError()
        return await asyncpg.connect(
            host=target.host,
            port=target.port,
            user=target.user,
            password=target.password,
            database=target.database,
            ssl=_ssl_context(target.sslmode) if encrypted else False,
            direct_tls=False,
            timeout=remaining,
            command_timeout=remaining,
            server_settings={"application_name": ACTION, "default_transaction_read_only": "on"},
        )

    try:
        return await connect(target.sslmode not in {"disable", "allow"})
    except ConnectionError as exc:
        if "rejected SSL upgrade" in str(exc):
            if target.sslmode == "prefer":
                return await connect(False)
            raise TLSNegotiationError() from None
        raise
    except asyncpg.InvalidAuthorizationSpecificationError as exc:
        if target.sslmode == "allow" and ("SSL off" in str(exc) or "no encryption" in str(exc)):
            return await connect(True)
        raise


def _warn(permissions: dict, code: str, message: str, **context):
    permissions["Warnings"].append({"Code": code, "Message": message, **context})


async def inspect_permissions(connection, request: DatabaseAccessRequest, result: dict):
    identity = await connection.fetchrow(IDENTITY_SQL)
    if identity is None:
        raise RuntimeError("Database identity unavailable")
    result.update(
        SessionUser=identity["session_user"],
        CurrentUser=identity["current_user"],
        IsSuperuser=identity["is_superuser"],
        Status="partial",
    )
    result["Database"] = {
        "Name": identity["database_name"],
        "Owner": identity["owner"],
        "IsOwner": identity["is_owner"],
        "HasOwnerPrivileges": identity["has_owner_privileges"],
    }
    if not identity["has_owner_privileges"]:
        _warn(result, "DATABASE_OWNER_PRIVILEGE_MISSING", "当前角色不具备数据库 owner 权限")
    schema = request.Schema if request.Schema is not None else identity["default_schema"]
    result["Schema"]["Name"] = schema
    for table in result["Tables"]:
        table["Schema"] = schema
    if schema is None:
        _warn(result, "SCHEMA_UNRESOLVED", "无法解析默认 Schema，请显式指定 Schema")
    else:
        row = await connection.fetchrow(SCHEMA_SQL, schema)
        result["Schema"]["Exists"] = row is not None
        if row:
            result["Schema"].update(
                Owner=row["owner"], CanUse=row["can_use"], CanCreate=row["can_create"]
            )
            if not row["can_use"]:
                _warn(
                    result, "SCHEMA_USAGE_DENIED", "当前角色没有 Schema USAGE 权限", Schema=schema
                )
            if not row["can_create"]:
                _warn(
                    result, "SCHEMA_CREATE_DENIED", "当前角色没有 Schema CREATE 权限", Schema=schema
                )
            if request.Tables:
                rows = {
                    r["name"]: r for r in await connection.fetch(TABLES_SQL, schema, request.Tables)
                }
                for table in result["Tables"]:
                    match = rows.get(table["Name"])
                    table["Exists"] = match is not None
                    if match is None:
                        _warn(
                            result,
                            "TABLE_NOT_FOUND",
                            "目标表不存在",
                            Schema=schema,
                            Table=table["Name"],
                        )
                    else:
                        table.update(
                            Owner=match["owner"],
                            IsOwner=match["is_owner"],
                            HasOwnerPrivileges=match["has_owner_privileges"],
                        )
                        if not match["has_owner_privileges"]:
                            _warn(
                                result,
                                "TABLE_OWNER_PRIVILEGE_MISSING",
                                "当前角色不具备目标表 owner 权限，建索引或修改表结构可能受限",
                                Schema=schema,
                                Table=table["Name"],
                            )
        else:
            _warn(result, "SCHEMA_NOT_FOUND", "目标 Schema 不存在", Schema=schema)
            for table in result["Tables"]:
                table["Exists"] = False
    if not request.Tables:
        _warn(result, "TABLE_OWNERSHIP_NOT_CHECKED", "已有表归属权限未检查")
    else:
        _warn(
            result,
            "DDL_REQUIREMENTS_NOT_EXHAUSTIVE",
            "检查结果仅反映对象归属条件，具体 DDL 可能还需要其他权限或条件",
        )
    result["Status"] = "complete" if schema is not None else "partial"


async def check_database_access(
    request: DatabaseAccessRequest, *, request_id: str
) -> dict[str, Any]:
    target = request.connection_target()
    started = time.monotonic()
    deadline = asyncio.get_running_loop().time() + request.TimeoutSeconds
    permissions = empty_permissions(request)
    result = {
        "AgentId": request.AgentId,
        "DatabaseType": "postgresql",
        "Success": False,
        "Connected": False,
        "Stage": "connect",
        "ReasonCode": "CONNECTION_FAILED",
        "Reason": "数据库连接失败",
        "DurationMs": 0,
        "PodName": os.getenv("HOSTNAME") or None,
        "Permissions": permissions,
    }
    connection = None
    error_meta = {}
    try:
        async with asyncio.timeout_at(deadline):
            connection = await connect_target(target, deadline)
            result.update(Connected=True, Stage="query")
            if await connection.fetchval("SELECT 1") != 1:
                raise RuntimeError("Unexpected query result")
            result.update(
                Success=True, Stage="completed", ReasonCode="OK", Reason="数据库连接及基础查询成功"
            )
            try:
                await inspect_permissions(connection, request, permissions)
            except Exception as exc:
                _warn(
                    permissions,
                    "PERMISSION_CHECK_UNAVAILABLE",
                    "归属及权限检查未完成，未知权限不代表权限不足",
                )
                error_meta = {
                    "error_type": type(exc).__name__,
                    "sqlstate": getattr(exc, "sqlstate", None),
                }
    except TimeoutError as exc:
        if result["Success"]:
            _warn(permissions, "PERMISSION_CHECK_TIMEOUT", "数据库连接成功，归属及权限检查超时")
        else:
            code, reason = classify_error(exc, result["Stage"])
            result.update(ReasonCode=code, Reason=reason)
        error_meta = {"error_type": type(exc).__name__}
    except Exception as exc:
        code, reason = classify_error(exc, result["Stage"])
        result.update(ReasonCode=code, Reason=reason)
        error_meta = {
            "error_type": type(exc).__name__,
            "sqlstate": getattr(exc, "sqlstate", None),
            "errno": getattr(exc, "errno", None),
        }
    finally:
        if connection is not None:
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0:
                    await asyncio.wait_for(connection.close(timeout=remaining), timeout=remaining)
                else:
                    connection.terminate()
            except BaseException:
                connection.terminate()
                if asyncio.current_task().cancelling():
                    raise
        result["DurationMs"] = round((time.monotonic() - started) * 1000)
    log = logger.info if result["Success"] else logger.warning
    log(
        "%s result %s",
        ACTION,
        json.dumps(
            {
                "request_id": request_id,
                "agent_id": request.AgentId,
                "host": target.host,
                "port": target.port,
                "database": target.database,
                "user": "***",
                "schema": permissions["Schema"]["Name"],
                "pod": result["PodName"],
                "success": result["Success"],
                "stage": result["Stage"],
                "reason_code": result["ReasonCode"],
                "duration_ms": result["DurationMs"],
                "permission_status": permissions["Status"],
                "warning_count": len(permissions["Warnings"]),
                **error_meta,
            },
            ensure_ascii=False,
        ),
    )
    return result
