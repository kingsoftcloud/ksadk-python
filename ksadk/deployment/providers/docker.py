"""
Docker Provider - 本地 Docker 部署
"""

import asyncio
import subprocess
import shutil
from pathlib import Path
from typing import Dict, Any, List

from ksadk.deployment.base import (
    BaseDeployProvider, 
    DeployTarget, 
    DeployResult, 
    DeployStatus, 
    PackageInfo
)
from ksadk.deployment.registry import DeployProviderRegistry


@DeployProviderRegistry.register("docker")
class DockerProvider(BaseDeployProvider):
    """本地 Docker 部署 Provider"""
    
    name = "docker"
    display_name = "Local Docker"
    description = "部署到本地 Docker 容器，用于开发测试"
    
    supports_streaming = False
    supports_scaling = False
    requires_image_registry = False
    
    async def validate_config(self, target: DeployTarget) -> tuple[bool, str]:
        """验证 Docker 是否可用"""
        try:
            result = subprocess.run(
                ["docker", "version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                return False, "Docker 未正常运行"
            return True, ""
        except FileNotFoundError:
            return False, "未找到 docker 命令，请确保已安装 Docker"
        except Exception as e:
            return False, f"Docker 检查失败: {e}"
    
    async def package(self, project_dir: str, detection_result: Any, config: Dict[str, Any] = None) -> PackageInfo:
        """打包项目"""
        project_path = Path(project_dir)
        output_dir = project_path / ".agentengine" / "build"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        package_name = Path(detection_result.package_path).name
        
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

        # 复制 ksadk 源码 (对齐 Code 模式，确保容器内可用)
        import ksadk
        ksadk_src = Path(ksadk.__file__).parent
        ksadk_dest = output_dir / "ksadk"
        if ksadk_dest.exists():
            shutil.rmtree(ksadk_dest)
        
        # 复制 ksadk 目录 (忽略 __pycache__ 等)
        shutil.copytree(
            ksadk_src, 
            ksadk_dest, 
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyd', '*.so', '*.dylib', '*.bin')
        )

        
        # 生成 Dockerfile
        dockerfile = self._generate_dockerfile(detection_result, config)
        dockerfile_path = output_dir / "Dockerfile"
        dockerfile_path.write_text(dockerfile)
        
        # 生成 requirements.txt (合并用户依赖)
        requirements = self._generate_requirements(detection_result, project_path)
        requirements_path = output_dir / "requirements.txt"
        requirements_path.write_text(requirements)
        
        # 生成启动脚本
        entrypoint = self._generate_entrypoint(detection_result, package_name)
        entrypoint_path = output_dir / "entrypoint.py"
        entrypoint_path.write_text(entrypoint)
        
        return PackageInfo(
            name=detection_result.name or project_path.name,
            framework=detection_result.type.value,
            build_dir=str(output_dir),
            project_dir=str(project_path),  # 保存原始项目目录
            dockerfile=str(dockerfile_path),
            entry_point=detection_result.entry_point,
            metadata={
                "package_name": package_name,
                "requirements": str(requirements_path),
                "entrypoint": str(entrypoint_path),
            }
        )
    
    async def build(self, package_info: PackageInfo, target: DeployTarget) -> PackageInfo:
        """构建 Docker 镜像"""
        import click
        
        image_name = f"agentengine/{package_info.name}:latest"
        
        click.echo("🔨 构建 Docker 镜像 (目标平台: linux/amd64)...")
        cmd = [
            "docker", "build",
            "--platform", "linux/amd64",  # 确保跨平台兼容
            "-t", image_name,
            "-f", package_info.dockerfile,
            package_info.build_dir
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            raise RuntimeError(f"Docker build failed: {result.stderr}")
        
        click.secho(f"✅ 镜像构建成功: {image_name}", fg="green")
        package_info.image = image_name
        return package_info
    
    async def deploy(self, package_info: PackageInfo, target: DeployTarget) -> DeployResult:
        """运行 Docker 容器"""
        container_name = f"{package_info.name}-container"
        port = target.extra.get("port", 8000)
        
        # 停止并删除旧容器 (如果存在)
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True
        )
        
        # 运行新容器
        cmd = [
            "docker", "run", "-d",
            "-p", f"{port}:8000",
            "--name", container_name,
            package_info.image
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            container_id = result.stdout.strip()[:12]
            return DeployResult(
                status=DeployStatus.RUNNING,
                agent_id=container_id,
                agent_name=package_info.name,
                endpoint=f"http://localhost:{port}",
                message=f"容器已启动: {container_name}",
                metadata={"container_name": container_name}
            )
        else:
            return DeployResult(
                status=DeployStatus.FAILED,
                agent_name=package_info.name,
                message=f"容器启动失败: {result.stderr}"
            )
    
    async def get_status(self, agent_id: str, target: DeployTarget) -> DeployResult:
        """获取容器状态"""
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", agent_id],
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            status = result.stdout.strip()
            deploy_status = {
                "running": DeployStatus.RUNNING,
                "exited": DeployStatus.STOPPED,
                "created": DeployStatus.PENDING,
            }.get(status, DeployStatus.UNKNOWN)
            
            return DeployResult(
                status=deploy_status,
                agent_id=agent_id,
                message=f"Container status: {status}"
            )
        else:
            return DeployResult(
                status=DeployStatus.UNKNOWN,
                agent_id=agent_id,
                message="Container not found"
            )
    
    async def destroy(self, agent_id: str, target: DeployTarget) -> bool:
        """删除容器"""
        result = subprocess.run(
            ["docker", "rm", "-f", agent_id],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    
    def _generate_dockerfile(self, detection_result: Any, config: Dict[str, Any] = None) -> str:
        """生成优化的 Dockerfile"""
        base_image = "python:3.12-slim"
        if config and 'build' in config and 'base_image' in config['build']:
            base_image = config['build']['base_image']
        
        # 优化点:
        # 1. 使用清华镜像源加速 pip 安装
        # 2. 分层构建: 先安装依赖再复制代码 (利用 Docker 缓存)
        # 3. 使用非 root 用户运行 (安全最佳实践)
        # 4. 正确的端口 8080 (Serverless 标准)
        # 5. 设置 PYTHONUNBUFFERED 确保日志实时输出
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
    
    def _generate_requirements(self, detection_result: Any, project_path: Path = None) -> str:
        """生成 requirements.txt (对齐 CodeBuilder)"""
        base_deps = [
            # Core
            "fastapi>=0.100.0",
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
        if framework == "adk":
            base_deps += ["google-adk>=0.1.0", "litellm>=1.0.0"]
        elif framework in ("langchain", "langgraph"):
            # LangChain 生态统一依赖
            base_deps += [
                "langchain>=0.1.0",
                "langchain-openai>=0.1.0",
                "langchain-core>=0.1.0",
                "langgraph>=0.1.0",
                # MCP 支持
                "mcp>=1.1.0",
                "langchain-mcp-adapters>=0.0.1",
            ]
        
        # 合并用户 requirements.txt (如果存在)
        if project_path:
            user_requirements = project_path / "requirements.txt"
            if user_requirements.exists():
                user_content = user_requirements.read_text()
                user_deps = [l.strip() for l in user_content.split('\n') if l.strip() and not l.startswith('#')]
                base_deps.extend(user_deps)
        
        return "\n".join(base_deps)
    
    def _generate_entrypoint(self, detection_result: Any, package_name: str) -> str:
        """生成 entrypoint.py (对齐 CodeBuilder)"""
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
        is_langchain = "{detection_result.type.name}" in ("LANGCHAIN", "LANGGRAPH")
        setup_tracing(use_callback_only=is_langchain)
        logger.info(f"Tracing 已启用 (Langfuse)")
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
