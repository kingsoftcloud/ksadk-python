"""
部署管理器
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Literal
from pathlib import Path
import shutil
import os

from ksadk.builders.framework_requirements import (
    FASTAPI_REQUIREMENT,
    minimal_requirements_for_framework,
)
from ksadk.builders.requirements_utils import merge_requirement_lists


class BaseDeployer(ABC):
    """部署器基类"""
    
    @abstractmethod
    def package(self, project_dir: str, detection_result: Any) -> Dict[str, Any]:
        """打包项目"""
        pass
    
    @abstractmethod
    def deploy(self, package_info: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """部署"""
        pass


class K8sDeployer(BaseDeployer):
    """K8s 部署器 (本地模拟)"""
    
    def package(self, project_dir: str, detection_result: Any) -> Dict[str, Any]:
        """打包项目并生成 K8s 配置"""
        project_path = Path(project_dir)
        output_dir = project_path / ".ksadk" / "deploy"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # 生成 Dockerfile
        dockerfile = self._generate_dockerfile(detection_result)
        dockerfile_path = output_dir / "Dockerfile"
        dockerfile_path.write_text(dockerfile)
        
        # 生成 requirements.txt
        requirements = self._generate_requirements(detection_result)
        requirements_path = output_dir / "requirements.txt"
        requirements_path.write_text(requirements)
        
        # 生成 K8s 部署配置
        k8s_config = self._generate_k8s_config()
        k8s_path = output_dir / "deployment.yaml"
        k8s_path.write_text(k8s_config)
        
        # 生成启动脚本
        entrypoint = self._generate_entrypoint(detection_result)
        entrypoint_path = output_dir / "entrypoint.py"
        entrypoint_path.write_text(entrypoint)
        
        return {
            "output_dir": str(output_dir),
            "dockerfile": str(dockerfile_path),
            "requirements": str(requirements_path),
            "config_path": str(k8s_path),
            "entrypoint": str(entrypoint_path),
            "framework": detection_result.type.value
        }
    
    def deploy(self, package_info: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """部署到 K8s (本地模拟)"""
        name = kwargs.get("name", "ksadk-agent")
        namespace = kwargs.get("namespace", "default")
        port = kwargs.get("port", 8000)
        
        # 检查 kubectl 是否可用
        import subprocess
        try:
            result = subprocess.run(
                ["kubectl", "version", "--client"],
                capture_output=True,
                text=True,
                timeout=10
            )
            kubectl_available = result.returncode == 0
        except Exception:
            kubectl_available = False
        
        if not kubectl_available:
            print("⚠️ kubectl 不可用，使用模拟部署模式")
            return {
                "status": "simulated",
                "endpoint": f"http://localhost:{port}",
                "message": "kubectl not available, deployment simulated",
                "name": name,
                "namespace": namespace
            }
        
        # 实际部署到 K8s
        config_path = package_info.get("config_path")
        if config_path:
            # 替换配置中的变量
            config_content = Path(config_path).read_text()
            config_content = config_content.replace("{{NAME}}", name)
            config_content = config_content.replace("{{NAMESPACE}}", namespace)
            config_content = config_content.replace("{{PORT}}", str(port))
            
            temp_config = Path(package_info["output_dir"]) / "deployment_final.yaml"
            temp_config.write_text(config_content)
            
            try:
                result = subprocess.run(
                    ["kubectl", "apply", "-f", str(temp_config)],
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                
                if result.returncode == 0:
                    return {
                        "status": "deployed",
                        "endpoint": f"http://{name}.{namespace}.svc.cluster.local:{port}",
                        "name": name,
                        "namespace": namespace,
                        "kubectl_output": result.stdout
                    }
                else:
                    return {
                        "status": "failed",
                        "error": result.stderr,
                        "name": name
                    }
            except Exception as e:
                return {
                    "status": "error",
                    "error": str(e),
                    "name": name
                }
        
        return {"status": "error", "message": "No config path provided"}
    
    def _generate_dockerfile(self, detection_result: Any) -> str:
        return f'''FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "entrypoint.py"]
'''
    
    def _generate_requirements(self, detection_result: Any) -> str:
        base_deps = [
            FASTAPI_REQUIREMENT,
            "uvicorn>=0.23.0",
            "python-dotenv>=1.0.0",
            "pydantic>=2.0.0",
        ]
        
        framework = detection_result.type.value
        base_deps += minimal_requirements_for_framework(framework)
        
        return "\n".join(merge_requirement_lists(base_deps))
    
    def _generate_k8s_config(self) -> str:
        return '''apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{NAME}}
  namespace: {{NAMESPACE}}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{NAME}}
  template:
    metadata:
      labels:
        app: {{NAME}}
    spec:
      containers:
      - name: agent
        image: {{NAME}}:latest
        imagePullPolicy: Never
        ports:
        - containerPort: {{PORT}}
        env:
        - name: OPENAI_API_BASE
          valueFrom:
            secretKeyRef:
              name: agent-secrets
              key: openai-api-base
              optional: true
        - name: OPENAI_API_KEY
          valueFrom:
            secretKeyRef:
              name: agent-secrets
              key: openai-api-key
              optional: true
---
apiVersion: v1
kind: Service
metadata:
  name: {{NAME}}
  namespace: {{NAMESPACE}}
spec:
  selector:
    app: {{NAME}}
  ports:
  - port: {{PORT}}
    targetPort: {{PORT}}
  type: ClusterIP
'''
    
    def _generate_entrypoint(self, detection_result: Any) -> str:
        package_name = Path(detection_result.package_path).name
        return f'''"""
KsADK 部署入口
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


class DockerDeployer(K8sDeployer):
    """Docker 部署器"""
    
    def deploy(self, package_info: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """构建并运行 Docker 容器"""
        import subprocess
        
        name = kwargs.get("name", "ksadk-agent")
        port = kwargs.get("port", 8000)
        output_dir = package_info.get("output_dir")
        
        # 构建镜像
        print(f"🔨 构建 Docker 镜像: {name}")
        try:
            result = subprocess.run(
                ["docker", "build", "-t", name, "-f", package_info["dockerfile"], output_dir],
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                return {"status": "failed", "error": result.stderr}
            
            print(f"✅ 镜像构建成功")
            
            # 运行容器
            result = subprocess.run(
                ["docker", "run", "-d", "-p", f"{port}:8000", "--name", f"{name}-container", name],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                return {
                    "status": "running",
                    "endpoint": f"http://localhost:{port}",
                    "container_id": result.stdout.strip()[:12],
                    "name": name
                }
            else:
                return {"status": "failed", "error": result.stderr}
                
        except Exception as e:
            return {"status": "error", "error": str(e)}


class DeploymentManager:
    """部署管理器工厂"""
    
    _deployers = {
        "k8s": K8sDeployer,
        "docker": DockerDeployer,
    }
    
    @classmethod
    def create(cls, target: Literal["k8s", "docker", "faas"]) -> BaseDeployer:
        """创建部署器"""
        if target == "faas":
            # FaaS 暂时使用 K8s 部署器模拟
            target = "k8s"
        
        deployer_class = cls._deployers.get(target)
        if not deployer_class:
            raise ValueError(f"不支持的部署目标: {target}")
        
        return deployer_class()
