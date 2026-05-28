"""
Container Builder - Docker 镜像构建
"""

import os
import subprocess
import shutil
import time
import platform
import asyncio
from pathlib import Path
from typing import Optional

import click

from ksadk.builders.base import BaseBuilder, BuildResult
from ksadk.builders.code_builder import CodeBuilder
from ksadk.builders.framework_requirements import (
    FASTAPI_REQUIREMENT,
    requirements_for_framework,
)
from ksadk.builders.requirements_utils import (
    exclude_requirement_names,
    merge_requirement_lists,
    parse_requirements_text,
)


def ensure_docker_running() -> bool:
    """确保 Docker 正在运行"""
    if not shutil.which('docker'):
        click.secho("❌ 未找到 docker 命令", fg='red')
        click.echo("")
        click.echo("请先安装 Docker:")
        if platform.system() == "Darwin":
            click.echo("  • 下载 Docker Desktop: https://www.docker.com/products/docker-desktop/")
            click.echo("  • 或使用 Homebrew: brew install --cask docker")
        elif platform.system() == "Linux":
            click.echo("  • Ubuntu/Debian: sudo apt-get install docker.io")
            click.echo("  • CentOS/RHEL: sudo yum install docker")
        else:
            click.echo("  • 下载 Docker Desktop: https://www.docker.com/products/docker-desktop/")
        return False
    
    try:
        result = subprocess.run(['docker', 'info'], capture_output=True, timeout=10)
        if result.returncode == 0:
            return True
    except:
        pass
    
    click.secho("⚠️  Docker daemon 未运行", fg='yellow')
    
    system_name = platform.system()
    if system_name == "Darwin":
        click.echo("🚀 正在启动 Docker Desktop...")
        try:
            subprocess.run(['open', '-a', 'Docker'], check=True)
            for i in range(60):
                time.sleep(1)
                try:
                    result = subprocess.run(['docker', 'info'], capture_output=True, timeout=5)
                    if result.returncode == 0:
                        click.secho("✅ Docker Desktop 已启动", fg='green')
                        return True
                except:
                    pass
                if i % 5 == 0 and i > 0:
                    click.echo(f"   等待 Docker 启动中... ({i}秒)")
            click.secho("❌ Docker Desktop 启动超时", fg='red')
            return False
        except:
            click.secho("❌ 无法启动 Docker Desktop", fg='red')
            return False
    elif system_name == "Windows":
        click.echo("请启动 Docker Desktop，然后重试当前命令。")
        return False
    else:
        click.echo("请启动 Docker daemon: sudo systemctl start docker")
        return False


class ContainerBuilder(BaseBuilder):
    """Docker 镜像构建器"""
    
    def __init__(self, project_dir: Path, config: dict = None,
                 tag: str = None, registry: str = None, no_cache: bool = False):
        super().__init__(project_dir, config)
        self.tag = tag
        self.registry = registry
        self.no_cache = no_cache
    
    def _get_smart_kcr_endpoint(self, region: str) -> str:
        """Return a public default registry namespace for container builds."""
        _ = region
        return "ghcr.io/kingsoftcloud/agentengine"
    
    def _optimize_kcr_endpoint(self, registry: str) -> str:
        """Return the configured registry unchanged."""
        return registry
    
    def _package(self, detection_result) -> 'PackageInfo':
        """打包项目 - 复制文件并生成 Dockerfile/requirements/entrypoint"""
        from ksadk.deployment.base import PackageInfo
        
        project_path = self.project_dir
        output_dir = project_path / ".agentengine" / "build"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        package_name = Path(detection_result.package_path).name
        is_container_first_template = (
            getattr(getattr(detection_result, "type", None), "value", "") == "hermes"
            and (project_path / "Dockerfile").exists()
            and (project_path / "entrypoint.sh").exists()
        )
        
        # 复制项目文件
        for item in project_path.iterdir():
            # 排除隐藏文件(但保留 .env*) 和特定忽略目录
            if (item.name.startswith('.') and not item.name.startswith('.env')) or item.name in ('__pycache__', '.git', 'node_modules'):
                continue
            dest = output_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(item, dest)
        
        # 复制 ksadk 源码 (确保容器内可用)
        import ksadk
        ksadk_src = Path(ksadk.__file__).parent
        ksadk_dest = output_dir / "ksadk"
        if ksadk_dest.exists():
            shutil.rmtree(ksadk_dest)

        def _ignore_ksadk_source(current_dir: str, names: list[str]):
            ignored = set(
                shutil.ignore_patterns(
                    "__pycache__",
                    "*.pyc",
                    "*.pyd",
                    "*.so",
                    "*.dylib",
                    "*.bin",
                )(current_dir, names)
            )
            relative_dir = Path(current_dir).resolve().relative_to(ksadk_src.resolve())
            if relative_dir == Path("server") and "web-ui" in names:
                ignored.add("web-ui")
            return ignored

        shutil.copytree(
            ksadk_src, 
            ksadk_dest, 
            ignore=_ignore_ksadk_source,
        )
        
        dockerfile_path = output_dir / "Dockerfile"
        if not is_container_first_template:
            dockerfile = self._generate_dockerfile(detection_result)
            dockerfile_path.write_text(dockerfile)
        
        # 生成 requirements.txt (合并用户依赖)
        requirements = self._generate_requirements(detection_result, project_path)
        requirements_path = output_dir / "requirements.txt"
        requirements_path.write_text(requirements)
        
        # 生成启动脚本
        if is_container_first_template:
            entrypoint_path = output_dir / "entrypoint.sh"
        else:
            entrypoint = self._generate_entrypoint(detection_result, package_name)
            entrypoint_path = output_dir / "entrypoint.py"
            entrypoint_path.write_text(entrypoint)
        
        return PackageInfo(
            name=detection_result.name or project_path.name,
            framework=detection_result.type.value,
            build_dir=str(output_dir),
            project_dir=str(project_path),
            dockerfile=str(dockerfile_path),
            entry_point=detection_result.entry_point,
            metadata={
                "package_name": package_name,
                "requirements": str(requirements_path),
                "entrypoint": str(entrypoint_path),
            }
        )
    
    def _generate_dockerfile(self, detection_result) -> str:
        """生成优化的 Dockerfile"""
        base_image = "python:3.12-slim"
        
        return f'''FROM {base_image}

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先复制依赖文件 (利用 Docker Layer 缓存)
COPY requirements.txt .

# 使用清华镜像源加速安装
RUN pip install -r requirements.txt -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

# 复制应用代码
COPY . .

# 创建非 root 用户 (安全最佳实践)
RUN useradd -m -u 1000 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

# 使用 exec 形式确保信号正确传递
CMD ["python", "entrypoint.py"]
'''
    
    def _generate_requirements(self, detection_result, project_path: Path = None) -> str:
        """生成 requirements.txt"""
        base_deps = [
            # Core
            FASTAPI_REQUIREMENT,
            "uvicorn>=0.23.0",
            "python-dotenv>=1.0.0",
            "pydantic>=2.0.0",
            "pyyaml>=6.0.0",
            "httpx>=0.24.0",
            # Tracing
            "opentelemetry-api>=1.37.0",
            "opentelemetry-sdk>=1.37.0",
            "opentelemetry-exporter-otlp>=1.37.0",
            "openinference-instrumentation-langchain>=0.1.0",
            "langfuse>=2.0.0",
        ]
        
        framework = detection_result.type.value
        base_deps += requirements_for_framework(framework)

        base_deps = merge_requirement_lists(
            base_deps,
            CodeBuilder(project_path or self.project_dir)._bundled_runtime_requirements(),
        )
        
        # 合并用户 requirements.txt (如果存在)
        if project_path:
            user_requirements = project_path / "requirements.txt"
            if user_requirements.exists():
                user_content = user_requirements.read_text()
                user_deps = exclude_requirement_names(
                    parse_requirements_text(user_content),
                    excluded_names={"ksadk"},
                )
                base_deps = merge_requirement_lists(base_deps, user_deps)
        
        return "\n".join(base_deps)
    
    def _generate_entrypoint(self, detection_result, package_name: str) -> str:
        """生成 entrypoint.py"""
        return f'''"""
AgentEngine Container 模式入口
"""

import sys
import os
import logging
from pathlib import Path

# ========== 日志配置 ==========
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)

logger = logging.getLogger("entrypoint")
logger.info(f"日志级别: {{LOG_LEVEL}}")

# 配置第三方库日志级别
if LOG_LEVEL != "DEBUG":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

# ========== 路径设置 ==========
sys.path.insert(0, "/app")
os.chdir("/app")

logger.info("=" * 60)
logger.info("AgentEngine 启动 (Container 模式)")
logger.info("=" * 60)
logger.info(f"Python: {{sys.version}}")

# 加载环境变量
try:
    from dotenv import load_dotenv
    if os.path.exists("/app/.env"):
        load_dotenv("/app/.env")
        logger.info("加载 .env 文件")
except ImportError:
    pass

# ========== 加载 Agent ==========
from ksadk.configs import setup_environment
setup_environment(Path("/app"))

try:
    from ksadk.runners.patch_langchain import apply_patch as apply_langchain_patch
    apply_langchain_patch()
except ImportError:
    pass

from ksadk.runners import create_runner
from ksadk.detection import DetectionResult, FrameworkType
from ksadk.server import app, set_runner
import uvicorn

# 检测结果 (构建时固化)
detection_result = DetectionResult(
    type=FrameworkType.{detection_result.type.name},
    name="{detection_result.name}",
    entry_point="{detection_result.entry_point}",
    package_path="/app/{package_name}",
    agent_variable="{detection_result.agent_variable}"
)

logger.info(f"框架: {{detection_result.name}}")
logger.info(f"入口: {{detection_result.entry_point}}")

# 初始化 Tracing (如果配置了 Langfuse)
if os.environ.get("LANGFUSE_PUBLIC_KEY"):
    try:
        from ksadk.tracing import setup_tracing
        use_callback_only = os.environ.get("LANGFUSE_USE_CALLBACK", "").strip().lower() in ("1", "true", "yes", "on")
        setup_tracing(use_callback_only=use_callback_only)
        logger.info(f"Tracing 已启用 (Langfuse, CallbackOnly={{use_callback_only}})")
    except Exception as e:
        logger.warning(f"Tracing 初始化失败: {{e}}")

# 创建 Runner 并加载 Agent
logger.info("正在加载 Agent...")
runner = create_runner(detection_result, "/app")
runner.load_agent()
set_runner(runner)
logger.info("Agent 加载成功!")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"启动 HTTP Server: 0.0.0.0:{{port}}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())
'''
    
    def build(self) -> BuildResult:
        """构建 Docker 镜像"""
        from ksadk.detection import FrameworkDetector
        
        self._load_dotenv()
        config = self._load_config()
        
        # 检测框架
        detector = FrameworkDetector(str(self.project_dir))
        result = detector.detect()
        
        if result.type.value == "unknown":
            return BuildResult(
                success=False,
                error_message="未检测到支持的框架"
            )
        
        click.echo(f"📦 框架: {click.style(result.name, fg='green')}")
        
        # 确定镜像名称
        image_name = config.get('name', self.project_dir.name).replace('-', '_').replace('.', '_')
        
        # Tag 优先级: 命令行 > agentengine.yaml version > config image.tag > latest
        image_tag = self.tag
        if not image_tag:
            image_tag = config.get('version', '')  # 使用项目版本作为 tag
        if not image_tag:
            image_tag = config.get('image', {}).get('tag', 'latest')
        
        # Registry 优先级: 命令行 > .env KCR_REGISTRY > agentengine.yaml > 公开默认 registry
        import os
        image_registry = self.registry
        if not image_registry:
            image_registry = os.getenv('KCR_REGISTRY', '')
        if not image_registry:
            image_registry = config.get('image', {}).get('registry', '')
        if not image_registry:
            image_registry = self._get_smart_kcr_endpoint(os.getenv('KSYUN_REGION', 'cn-beijing-6'))
        else:
            image_registry = self._optimize_kcr_endpoint(image_registry)
        
        full_image = f"{image_registry}/{image_name}:{image_tag}"
        
        click.echo(f"🏷️  镜像名称: {full_image}")
        
        # 打包 (内置打包逻辑，不再依赖 DockerProvider)
        click.echo("\n📦 打包中...")
        try:
            package_info = self._package(result)
            click.echo("✅ 打包完成")
        except Exception as e:
            return BuildResult(
                success=False,
                error_message=f"打包失败: {e}"
            )
        
        # Docker 构建
        if not ensure_docker_running():
            return BuildResult(
                success=False,
                error_message="Docker 未运行"
            )
        
        click.echo("\n🔨 构建 Docker 镜像 (目标平台: linux/amd64)...")
        try:
            cmd = ['docker', 'build', '--platform', 'linux/amd64', '-t', full_image]
            if self.no_cache:
                cmd.append('--no-cache')
            cmd.append(package_info.build_dir)

            quiet_mode = os.getenv("AGENTENGINE_OUTPUT_MODE", "").strip().lower() == "json"
            subprocess.run(
                cmd,
                check=True,
                capture_output=quiet_mode,
                text=quiet_mode,
            )
            click.secho(f"\n✅ 镜像构建成功: {full_image}", fg='green')
            
            return BuildResult(
                success=True,
                artifact_path=None,  # Docker 镜像没有本地文件路径
                metadata={
                    "image": full_image,
                    "framework": result.type.value,
                    "build_dir": package_info.build_dir
                }
            )
        except subprocess.CalledProcessError as e:
            error_message = f"镜像构建失败: {e}"
            if quiet_mode:
                stderr = (e.stderr or "").strip()
                stdout = (e.stdout or "").strip()
                detail = stderr or stdout
                if detail:
                    error_message = f"{error_message}: {detail}"
            return BuildResult(
                success=False,
                error_message=error_message
            )
    
    def push(self, image_name: str) -> bool:
        """推送镜像到仓库"""
        # 检查镜像仓库认证
        registry = self._extract_registry(image_name)
        
        # 尝试使用 .env 中的认证信息自动登录
        if registry:
            if not self._auto_login_from_env(registry):
                # 自动登录失败，检查是否已有认证
                if not self._check_registry_auth(registry):
                    return False
        
        click.echo(f"\n📤 推送镜像...")
        try:
            result = subprocess.run(
                ['docker', 'push', image_name], 
                capture_output=True, 
                text=True
            )
            if result.returncode != 0:
                # 检查是否是认证问题
                if 'denied' in result.stderr.lower() or 'unauthorized' in result.stderr.lower():
                    self._print_auth_help(registry or 'docker.io')
                    return False
                click.secho(f"❌ 镜像推送失败: {result.stderr}", fg='red')
                return False
            
            click.secho(f"✅ 镜像推送成功", fg='green')
            return True
        except subprocess.CalledProcessError as e:
            click.secho(f"❌ 镜像推送失败: {e}", fg='red')
            return False
    
    def _auto_login_from_env(self, registry: str) -> bool:
        """尝试使用 .env 中的凭证自动登录"""
        import os
        from dotenv import load_dotenv
        
        # 加载 .env
        load_dotenv()
        
        # KCR_REGISTRY 默认使用公开占位 registry
        kcr_registry = os.getenv('KCR_REGISTRY', '')
        if not kcr_registry:
            kcr_registry = "ghcr.io/kingsoftcloud/agentengine"
        
        # KCR_USERNAME 默认使用 KSYUN_ACCOUNT_ID
        kcr_username = os.getenv('KCR_USERNAME', '') or os.getenv('KSYUN_ACCOUNT_ID', '')
        kcr_password = os.getenv('KCR_PASSWORD', '')
        
        # 检查是否匹配当前 registry (支持部分匹配)
        if registry not in kcr_registry and kcr_registry not in registry:
            return False
        
        if not kcr_username or not kcr_password:
            return False
        
        click.echo(f"🔐 使用 .env 中的凭证登录 {registry}...")
        try:
            result = subprocess.run(
                ['docker', 'login', registry, '-u', kcr_username, '--password-stdin'],
                input=kcr_password,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                click.secho(f"✅ 登录成功", fg='green')
                return True
            else:
                click.secho(f"⚠️  自动登录失败: {result.stderr.strip()}", fg='yellow')
                return False
        except Exception as e:
            click.secho(f"⚠️  自动登录异常: {e}", fg='yellow')
            return False
    
    def get_registry_credentials(self) -> dict:
        """获取镜像仓库凭证 (用于传给 Serverless)
        
        返回扁平化结构: {"username": "...", "password": "..."}
        """
        import os
        from dotenv import load_dotenv
        
        load_dotenv()
        
        # KCR_USERNAME 默认使用 KSYUN_ACCOUNT_ID
        username = os.getenv('KCR_USERNAME', '') or os.getenv('KSYUN_ACCOUNT_ID', '')
        password = os.getenv('KCR_PASSWORD', '')
        
        if username and password:
            return {
                'username': username,
                'password': password
            }
        return {}
    
    def _extract_registry(self, image_name: str) -> Optional[str]:
        """从镜像名称提取仓库地址"""
        # 格式: registry/namespace/image:tag 或 namespace/image:tag (默认 docker.io)
        parts = image_name.split('/')
        if len(parts) >= 2 and ('.' in parts[0] or ':' in parts[0]):
            return parts[0]
        return None  # 使用默认 docker.io
    
    def _check_registry_auth(self, registry: str) -> bool:
        """检查是否已登录镜像仓库"""
        try:
            # 尝试获取 docker 配置
            import json
            from pathlib import Path
            
            docker_config = Path.home() / '.docker' / 'config.json'
            if docker_config.exists():
                with open(docker_config) as f:
                    config = json.load(f)
                    auths = config.get('auths', {})
                    # 检查是否有该仓库的认证信息
                    if registry in auths or f'https://{registry}' in auths:
                        return True
            
            # 没有找到认证信息，提示用户
            click.secho(f"\n⚠️  未检测到 {registry} 的登录凭证", fg='yellow')
            self._print_auth_help(registry)
            return False
        except Exception:
            # 无法检查，继续尝试推送
            return True
    
    def _print_auth_help(self, registry: str):
        """打印认证帮助信息"""
        click.echo("")
        click.echo("🔐 请先登录镜像仓库:")
        click.echo("")
        
        if 'kce.ksyun.com' in registry or 'hub-' in registry:
            # 金山云 KCR
            click.echo(f"   # 金山云容器镜像服务 (KCR)")
            click.echo(f"   docker login {registry}")
            click.echo("")
            click.echo("   用户名: 您的金山云账号 ID")
            click.echo("   密码: 在 KCR 控制台获取临时密码")
            click.echo("")
            click.echo("   获取密码: https://kcr.console.ksyun.com/ → 访问凭证")
        elif 'docker.io' in registry or registry == '':
            # Docker Hub
            click.echo("   # Docker Hub")
            click.echo("   docker login")
            click.echo("")
            click.echo("   提示: 需要先在 https://hub.docker.com 注册账号")
        else:
            # 其他仓库
            click.echo(f"   docker login {registry}")
        
        click.echo("")
        click.echo("💡 配置 CLI 默认仓库:")
        click.echo(f"   agentengine config set defaults.registry {registry}")
        click.echo("")
        click.echo("   配置后构建将自动使用该仓库:")  
        click.echo(f"   agentengine build --mode container --push")
