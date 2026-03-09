"""
KS3 上传模块 - 金山云对象存储上传
"""

import os
from pathlib import Path
from typing import Optional, Tuple
import click


from ksadk.common.constants import get_ks3_endpoints
from ksadk.configs.settings import check_endpoint_reachable

# KS3 Region 映射表 已移动到 ksadk.common.constants


class KS3Uploader:
    """KS3 上传器"""

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
        """根据 region 获取合适的 endpoint (自动选择内网或公网)"""
        if self.custom_domain:
            return self.custom_domain

        # 获取 endpoints (public, internal)
        public_endpoint, internal_endpoint = get_ks3_endpoints(self.region)

        # 使用统一的网络检测函数检查内网是否可达
        if internal_endpoint:
            click.echo(f"   检测内网连接: {internal_endpoint} ...", nl=False)
            if check_endpoint_reachable(internal_endpoint, port=443):
                click.secho(" ✓ 可用 (使用内网加速)", fg="green")
                return internal_endpoint
            else:
                click.secho(" ✗ 不可用", fg="yellow")

        return public_endpoint

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

        # 获取 KS3 endpoint
        ks3_host = self.get_endpoint()
        click.echo(f"   KS3 Endpoint: {ks3_host}")

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
            from ks3.connection import Connection

            conn = Connection(ak, sk, host=ks3_host)

            # 检查 bucket 是否真的存在
            # 注意: get_bucket() 只是返回一个对象，不会验证存在性
            # 需要实际调用 API 来确认
            bucket = conn.get_bucket(self.bucket_name)
            bucket_exists = False
            
            try:
                # 尝试列出 bucket（限制1个对象），这会真正验证 bucket 是否存在
                list(bucket.list(max_keys=1))
                bucket_exists = True
                click.echo(f"   ✓ Bucket 已存在: {self.bucket_name}")
            except Exception as e:
                # Bucket 不存在或无权限访问
                error_str = str(e)
                if "NoSuchBucket" in error_str or "404" in error_str:
                    click.echo(f"   Bucket 不存在，正在创建: {self.bucket_name}")
                    bucket_exists = False
                else:
                    # 其他错误（如权限问题）
                    click.secho(f"   ✗ 检查 Bucket 失败: {e}", fg="red")
                    if "AccessDenied" in error_str or "403" in error_str:
                        click.secho(f"   ⚠️  提示: Bucket '{self.bucket_name}' 名称冲突或无权限访问 (403)。", fg="yellow")
                        click.secho(f"      注意: KS3 Bucket 名称在全网范围内是全局唯一的！", fg="yellow")
                        click.secho(f"      该名称已被其他用户占用，您无法使用。", fg="yellow")
                        click.secho(f"   👉 解决方案:", fg="cyan")
                        click.secho(f"      1. (推荐) 在 .env 中设置 KSYUN_ACCOUNT_ID 为您的账户 ID (自动生成唯一名称)。", fg="cyan")
                        click.secho(f"      2. 或者，在 .env 中设置 KS3_BUCKET 为一个没人用过的唯一名称。", fg="cyan")
                    return None
            
            # 如果 bucket 不存在，创建它
            if not bucket_exists:
                try:
                    bucket = conn.create_bucket(self.bucket_name)
                    click.secho(f"   ✓ Bucket 创建成功: {self.bucket_name}", fg="green")
                except Exception as create_err:
                    click.secho(f"   ✗ Bucket 创建失败: {create_err}", fg="red")
                    
                    # 针对名称冲突 (Conflict) 的专门提示
                    create_err_str = str(create_err)
                    if "Conflict" in create_err_str or "409" in create_err_str or "BucketAlreadyExists" in create_err_str:
                         click.secho(f"   ⚠️  提示: Bucket '{self.bucket_name}' 名称已被其他用户占用。", fg="yellow")
                         click.secho(f"      注意: KS3 Bucket 名称是全局唯一的。", fg="yellow")
                         click.secho(f"   👉 解决方法: 修改 .env 中的 KS3_BUCKET，换一个更复杂的名字再试。", fg="cyan")
                    click.echo(f"   提示: 请检查以下问题:")
                    click.echo(f"      1. AK/SK 是否有创建 bucket 的权限")
                    click.echo(f"      2. Bucket 名称 '{self.bucket_name}' 是否已被其他账号占用")
                    click.echo(f"      3. Region '{self.region}' 配置是否正确")
                    click.echo(f"   临时方案: 使用 --ks3-bucket 参数指定已存在的 bucket")
                    return None

            # 上传 (设置为公开可读，方便调试)
            key = bucket.new_key(object_key)
            result = key.set_contents_from_filename(str(file_path), policy="public-read")

            if result and result.status == 200:
                return f"ks3://{self.bucket_name}/{object_key}"
            else:
                click.secho(f"   上传返回: {result}", fg="yellow")
                return None


        except ImportError:
            click.secho("❌ ks3sdk 导入失败，请确保已安装: pip install ksadk[runtime]", fg="red")
            return None
        except Exception as e:
            click.secho(f"❌ KS3 上传失败: {e}", fg="red")
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
