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
            if item.name.startswith('.') or item.name in ('__pycache__', '.git', 'node_modules'):
                continue
            dest = output_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(item, dest)
        
        # 生成 Dockerfile
        dockerfile = self._generate_dockerfile(detection_result, config)
        dockerfile_path = output_dir / "Dockerfile"
        dockerfile_path.write_text(dockerfile)
        
        # 生成 requirements.txt
        requirements = self._generate_requirements(detection_result)
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
        image_name = f"agentengine/{package_info.name}:latest"
        
        cmd = [
            "docker", "build",
            "-t", image_name,
            "-f", package_info.dockerfile,
            package_info.build_dir
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            raise RuntimeError(f"Docker build failed: {result.stderr}")
        
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
        base_image = "python:3.11-slim"
        if config and 'build' in config and 'base_image' in config['build']:
            base_image = config['build']['base_image']
            
        return f'''FROM {base_image}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "entrypoint.py"]
'''
    
    def _generate_requirements(self, detection_result: Any) -> str:
        base_deps = [
            "fastapi>=0.100.0",
            "uvicorn>=0.23.0",
            "python-dotenv>=1.0.0",
            "pydantic>=2.0.0",
        ]
        
        framework = detection_result.type.value
        if framework == "adk":
            base_deps += ["google-adk>=1.0.0", "litellm>=1.0.0"]
        elif framework == "langchain":
            base_deps += ["langchain>=0.1.0", "langchain-openai>=0.1.0"]
        elif framework == "langgraph":
            base_deps += ["langgraph>=0.1.0", "langchain-openai>=0.1.0"]
        
        return "\n".join(base_deps)
    
    def _generate_entrypoint(self, detection_result: Any, package_name: str) -> str:
        return f'''"""
AgentEngine 部署入口
"""

import sys
import os

sys.path.insert(0, "/app")

# 加载环境变量
try:
    from dotenv import load_dotenv
    if os.path.exists("/app/.env"):
        load_dotenv("/app/.env")
except ImportError:
    pass

from ksadk.runners import create_runner
from ksadk.detection import DetectionResult, FrameworkType
from ksadk.server import app, set_runner
import uvicorn

# 检测结果 (部署时固化)
detection_result = DetectionResult(
    type=FrameworkType.{detection_result.type.name},
    name="{detection_result.name}",
    entry_point="{detection_result.entry_point}",
    package_path="/app/{package_name}",
    agent_variable="{detection_result.agent_variable}"
)

# 创建 Runner 并加载 Agent
runner = create_runner(detection_result, "/app")
runner.load_agent()

# 设置 Runner 到 FastAPI app
set_runner(runner)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
'''
