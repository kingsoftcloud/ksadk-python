"""
KS3 上传模块 - 金山云对象存储上传
"""

import os
import socket
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
import click

from ks3.upload import UploadTask


from ksadk.common.constants import get_ks3_endpoints

# KS3 Region 映射表 已移动到 ksadk.common.constants


class KS3Uploader:
    """KS3 上传器"""

    ENDPOINT_PROBE_TIMEOUT_SECONDS = 1.5
    UPLOAD_CONNECT_PORT = 443
    UPLOAD_CONNECT_TIMEOUT_SECONDS = 60
    UPLOAD_TIMEOUT_BASE_SECONDS = 300
    UPLOAD_TIMEOUT_PER_MB_SECONDS = 4.0
    UPLOAD_TIMEOUT_MAX_SECONDS = 3600

    def __init__(self, region: str = "cn-beijing-6", bucket: str = None):
        """初始化 KS3 上传器
        
        Args:
            region: KS3 区域 (默认 cn-beijing-6)
            bucket: bucket 名称 (可选)
                   - 如果指定，使用指定的 bucket
                   - 如果未指定，优先从环境变量 KS3_BUCKET 读取
                   - 如果环境变量也未设置，默认使用 agentengine-{region}
        """
        self.region = region
        
        # 确定 bucket 名称 (优先级: 参数 > 环境变量 > 默认值)
        if bucket:
            self.bucket_name = bucket
        elif os.getenv("KS3_BUCKET"):
            self.bucket_name = os.getenv("KS3_BUCKET")
        else:
            # Bucket 名称格式: agentengine-{account_id}-{region}
            account_id = os.getenv("KSYUN_ACCOUNT_ID")
            if not account_id:
                raise ValueError(
                    "❌ 缺少 KSYUN_ACCOUNT_ID 环境变量\n"
                    "   Bucket 名称格式必须为: agentengine-{account_id}-{region}\n"
                    "   请在 .env 文件中设置: KSYUN_ACCOUNT_ID=你的账号ID"
                )
            self.bucket_name = f"agentengine-{account_id}-{region}"
        
        self.custom_domain = None  # 可选的自定义域名

    def get_endpoint(self) -> str:
        """根据 region 获取首选 endpoint (自动测速/回退)。"""
        targets, _summary = self._rank_upload_endpoints()
        return targets[0]["host"]

    def _endpoint_mode(self) -> str:
        mode = (os.getenv("KS3_ENDPOINT_MODE") or "auto").strip().lower()
        if mode in {"auto", "internal", "public"}:
            return mode
        return "auto"

    def _endpoint_probe_timeout_seconds(self) -> float:
        configured = os.getenv("KS3_ENDPOINT_PROBE_TIMEOUT_SECONDS")
        if configured:
            try:
                return max(0.2, float(configured))
            except ValueError:
                pass
        return self.ENDPOINT_PROBE_TIMEOUT_SECONDS

    def _upload_timeout_seconds(self, file_path: Path) -> int:
        configured = os.getenv("KS3_UPLOAD_TIMEOUT_SECONDS")
        if configured:
            try:
                return max(30, int(float(configured)))
            except ValueError:
                pass

        size_mb = file_path.stat().st_size / (1024 * 1024)
        calculated = self.UPLOAD_TIMEOUT_BASE_SECONDS + int(size_mb * self.UPLOAD_TIMEOUT_PER_MB_SECONDS)
        return min(self.UPLOAD_TIMEOUT_MAX_SECONDS, max(self.UPLOAD_CONNECT_TIMEOUT_SECONDS, calculated))

    def _probe_endpoint_latency(self, host: str) -> Optional[float]:
        started_at = time.monotonic()
        try:
            with socket.create_connection(
                (host, self.UPLOAD_CONNECT_PORT),
                timeout=self._endpoint_probe_timeout_seconds(),
            ):
                return time.monotonic() - started_at
        except OSError:
            return None

    def _rank_upload_endpoints(self) -> tuple[list[dict[str, str]], str]:
        if self.custom_domain:
            return (
                [{"host": self.custom_domain, "label": "自定义域名"}],
                f"使用自定义域名 {self.custom_domain}",
            )

        public_endpoint, internal_endpoint = get_ks3_endpoints(self.region)
        mode = self._endpoint_mode()

        candidates: list[dict[str, str]] = []
        seen: set[str] = set()

        def add_candidate(host: Optional[str], label: str) -> None:
            normalized = (host or "").strip()
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append({"host": normalized, "label": label})

        if mode == "internal":
            add_candidate(internal_endpoint, "内网")
            add_candidate(public_endpoint, "公网")
            return candidates, f"强制优先内网 {candidates[0]['host']}" if candidates else "未找到可用 KS3 endpoint"

        if mode == "public":
            add_candidate(public_endpoint, "公网")
            add_candidate(internal_endpoint, "内网")
            return candidates, f"强制优先公网 {candidates[0]['host']}" if candidates else "未找到可用 KS3 endpoint"

        add_candidate(internal_endpoint, "内网")
        add_candidate(public_endpoint, "公网")
        if not candidates:
            return [], "未找到可用 KS3 endpoint"

        timings: dict[str, Optional[float]] = {}
        with ThreadPoolExecutor(max_workers=min(2, len(candidates))) as executor:
            future_to_host = {
                executor.submit(self._probe_endpoint_latency, item["host"]): item["host"]
                for item in candidates
            }
            for future in as_completed(future_to_host):
                host = future_to_host[future]
                try:
                    timings[host] = future.result()
                except Exception:
                    timings[host] = None

        reachable = [item for item in candidates if timings.get(item["host"]) is not None]
        unreachable = [item for item in candidates if timings.get(item["host"]) is None]

        if not reachable:
            return candidates, "测速失败，按默认顺序尝试内外网端点"

        ordered = sorted(reachable, key=lambda item: timings[item["host"]] or float("inf"))
        best = ordered[0]
        best_ms = round((timings[best["host"]] or 0.0) * 1000)
        skipped = ""
        if unreachable:
            skipped_hosts = ", ".join(item["host"] for item in unreachable)
            skipped = f"，跳过不可达端点 {skipped_hosts}"
        return ordered, f"测速优先 {best['label']} {best['host']} ({best_ms} ms){skipped}"

    def _ensure_bucket(self, conn):
        bucket = conn.get_bucket(self.bucket_name)
        bucket_exists = False

        try:
            list(bucket.list(max_keys=1))
            bucket_exists = True
            click.echo(f"   ✓ Bucket 已存在: {self.bucket_name}")
        except Exception as e:
            error_str = str(e)
            if "NoSuchBucket" in error_str or "404" in error_str:
                click.echo(f"   Bucket 不存在，正在创建: {self.bucket_name}")
                bucket_exists = False
            else:
                if "AccessDenied" in error_str or "403" in error_str:
                    click.secho(f"   ⚠️  提示: Bucket '{self.bucket_name}' 名称冲突或无权限访问 (403)。", fg="yellow")
                    click.secho("      注意: KS3 Bucket 名称在全网范围内是全局唯一的！", fg="yellow")
                    click.secho("      该名称已被其他用户占用，您无法使用。", fg="yellow")
                    click.secho("   👉 解决方案:", fg="cyan")
                    click.secho("      1. (推荐) 在 .env 中设置 KSYUN_ACCOUNT_ID 为您的账户 ID (自动生成唯一名称)。", fg="cyan")
                    click.secho("      2. 或者，在 .env 中设置 KS3_BUCKET 为一个没人用过的唯一名称。", fg="cyan")
                raise

        if not bucket_exists:
            try:
                bucket = conn.create_bucket(self.bucket_name)
                click.secho(f"   ✓ Bucket 创建成功: {self.bucket_name}", fg="green")
            except Exception as create_err:
                create_err_str = str(create_err)
                if "Conflict" in create_err_str or "409" in create_err_str or "BucketAlreadyExists" in create_err_str:
                    click.secho(f"   ⚠️  提示: Bucket '{self.bucket_name}' 名称已被其他用户占用。", fg="yellow")
                    click.secho("      注意: KS3 Bucket 名称是全局唯一的。", fg="yellow")
                    click.secho("   👉 解决方法: 修改 .env 中的 KS3_BUCKET，换一个更复杂的名字再试。", fg="cyan")
                raise

        return bucket

    @staticmethod
    def _should_use_resumable_upload(file_path: Path) -> bool:
        try:
            return file_path.stat().st_size > 100 * 1024 * 1024
        except OSError:
            return False

    @staticmethod
    def _resumable_record_path(file_path: Path, object_key: str) -> Path:
        safe_key = object_key.strip().replace("/", "_").replace("\\", "_")
        record_dir = file_path.parent / ".agentengine" / "ks3_resume"
        record_dir.mkdir(parents=True, exist_ok=True)
        return record_dir / f"{safe_key}.ks3resume"

    def _upload_via_host(self, file_path: Path, object_key: str, host: str) -> bool:
        from ks3.connection import Connection

        ak = os.environ.get("KSYUN_ACCESS_KEY") or os.environ.get("KS3_ACCESS_KEY")
        sk = os.environ.get("KSYUN_SECRET_KEY") or os.environ.get("KS3_SECRET_KEY")

        conn = Connection(
            ak,
            sk,
            host=host,
            port=self.UPLOAD_CONNECT_PORT,
            is_secure=True,
            timeout=self._upload_timeout_seconds(file_path),
        )

        bucket = self._ensure_bucket(conn)

        key = bucket.new_key(object_key)
        if self._should_use_resumable_upload(file_path):
            worker_count = max(2, min(8, (os.cpu_count() or 4)))
            resume_path = self._resumable_record_path(file_path, object_key)
            click.echo(
                f"   启用断点续传: {file_path.stat().st_size / (1024 * 1024):.2f} MB, "
                f"workers={worker_count}"
            )
            click.echo(f"   续传记录: {resume_path}")
            upload_task = UploadTask(
                key,
                bucket,
                str(file_path),
                executor=ThreadPoolExecutor(max_workers=worker_count),
                resumable=True,
                resumable_filename=str(resume_path),
            )
            result = upload_task.upload(headers={"x-kss-acl": "public-read"})
        else:
            click.echo(f"   普通上传: {file_path.stat().st_size / (1024 * 1024):.2f} MB")
            result = key.set_contents_from_filename(str(file_path), policy="public-read")

        status = getattr(result, "status", None)
        if status is None:
            response_metadata = getattr(result, "response_metadata", None)
            status = getattr(response_metadata, "status", None)
        return bool(status == 200)

    async def upload(self, file_path: Path, object_key: str) -> Optional[str]:
        """上传文件到 KS3

        Args:
            file_path: 本地文件路径
            object_key: KS3 对象键 (如 agents/my_agent/code.zip)

        Returns:
            成功返回 ks3:// URI, 失败返回 None
        """
        # 检查环境变量 (优先使用 KSYUN_* 金山云 IAM 凭证)
        ak = os.environ.get("KSYUN_ACCESS_KEY") or os.environ.get("KS3_ACCESS_KEY")
        sk = os.environ.get("KSYUN_SECRET_KEY") or os.environ.get("KS3_SECRET_KEY")

        if not ak or not sk:
            click.secho("❌ 请在 .env 文件中设置金山云 IAM 凭证:", fg="red")
            click.echo("   KSYUN_ACCESS_KEY=your_access_key")
            click.echo("   KSYUN_SECRET_KEY=your_secret_key")
            return None

        upload_targets, selection_summary = self._rank_upload_endpoints()
        if not upload_targets:
            click.secho("❌ 未找到可用的 KS3 Endpoint", fg="red")
            return None

        click.echo(f"   KS3 Endpoint 策略: {selection_summary}")
        click.echo(f"   上传文件: {file_path.name} ({file_path.stat().st_size / (1024 * 1024):.2f} MB)")
        click.echo(f"   上传超时: {self._upload_timeout_seconds(file_path)} 秒")

        # 临时禁用系统代理 (ClashX 等会导致 KS3 上传走代理而失败)
        proxy_env_vars = [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ]
        saved_proxies = {}
        for var in proxy_env_vars:
            if var in os.environ:
                saved_proxies[var] = os.environ.pop(var)

        try:
            from ks3.connection import Connection as _Connection  # noqa: F401
        except ImportError:
            click.secho("❌ ks3sdk 导入失败，请确保已安装: pip install ksadk[runtime]", fg="red")
            return None

        try:
            last_error: Optional[Exception] = None
            for index, target in enumerate(upload_targets, start=1):
                host = target["host"]
                label = target["label"]
                click.echo(f"   尝试上传 [{index}/{len(upload_targets)}]: {label} {host}")
                try:
                    if self._upload_via_host(file_path, object_key, host):
                        click.secho(f"   ✓ 上传成功 ({label})", fg="green")
                        return f"ks3://{self.bucket_name}/{object_key}"
                    click.secho(f"   ⚠ 上传未确认成功，准备切换其他端点: {host}", fg="yellow")
                except Exception as e:
                    last_error = e
                    click.secho(f"   ⚠ {label} 上传失败: {e}", fg="yellow")

            if last_error is not None:
                click.secho(f"❌ KS3 上传失败: {last_error}", fg="red")
            else:
                click.secho("❌ KS3 上传失败: 所有端点均未返回成功状态", fg="red")
            return None
        finally:
            # 恢复代理环境变量
            for var, val in saved_proxies.items():
                os.environ[var] = val

    @staticmethod
    def _normalize_object_key(object_key: str) -> str:
        """规范化 object_key，避免 URL 出现双斜杠。"""
        return object_key.lstrip("/")

    def get_public_url_by_key(self, object_key: str) -> str:
        """获取指定 object_key 的公网访问 URL。"""
        endpoint, _ = get_ks3_endpoints(self.region)
        key = self._normalize_object_key(object_key)
        return f"https://{self.bucket_name}.{endpoint}/{key}"

    def get_internal_url_by_key(self, object_key: str) -> str:
        """获取指定 object_key 的内网访问 URL。"""
        _, endpoint = get_ks3_endpoints(self.region)
        if not endpoint:
            endpoint = f"ks3-internal.{self.region}.ksyun.com"
        key = self._normalize_object_key(object_key)
        return f"https://{self.bucket_name}.{endpoint}/{key}"

    async def upload_with_url(self, file_path: Path, presigned_url: str) -> bool:
        """使用预签名 URL 上传文件 (不依赖本地 AK/SK)"""
        import httpx
        
        click.echo(f"   上传中 (使用预签名 URL)...")
        
        try:
            async with httpx.AsyncClient() as client:
                with open(file_path, 'rb') as f:
                    response = await client.put(
                        presigned_url, 
                        content=f.read(),
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=300.0
                    )
                    response.raise_for_status()
                    click.secho("   ✓ 上传成功", fg="green")
                    return True
        except Exception as e:
            click.secho(f"❌ 上传失败: {e}", fg="red")
            return False
