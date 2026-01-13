"""
K8s Provider - Kubernetes 集群部署
"""

import subprocess
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
from ksadk.deployment.providers.docker import DockerProvider


@DeployProviderRegistry.register("k8s")
class K8sProvider(DockerProvider):
    """Kubernetes 集群部署 Provider
    
    继承 DockerProvider 的打包和构建能力，
    增加 K8s 部署能力。
    """
    
    name = "k8s"
    display_name = "Kubernetes Cluster"
    description = "部署到 Kubernetes 集群 (用户自有集群或托管集群)"
    
    supports_streaming = True
    supports_scaling = True
    requires_image_registry = True
    
    async def validate_config(self, target: DeployTarget) -> tuple[bool, str]:
        """验证 kubectl 是否可用"""
        # 先检查 Docker
        docker_ok, docker_msg = await super().validate_config(target)
        if not docker_ok:
            return docker_ok, docker_msg
        
        # 检查 kubectl
        try:
            kubeconfig = target.extra.get("kubeconfig")
            cmd = ["kubectl", "version", "--client"]
            if kubeconfig:
                cmd.extend(["--kubeconfig", kubeconfig])
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return False, "kubectl 未正确配置"
            return True, ""
        except FileNotFoundError:
            return False, "未找到 kubectl 命令"
        except Exception as e:
            return False, f"kubectl 检查失败: {e}"
    
    async def package(self, project_dir: str, detection_result: Any, config: Dict[str, Any] = None) -> PackageInfo:
        """打包项目并生成 K8s 配置"""
        # 先调用父类打包
        package_info = await super().package(project_dir, detection_result, config)
        
        # 生成 K8s 配置
        k8s_config = self._generate_k8s_manifest()
        k8s_path = Path(package_info.build_dir) / "k8s-deployment.yaml"
        k8s_path.write_text(k8s_config)
        
        package_info.metadata["k8s_manifest"] = str(k8s_path)
        return package_info
    
    async def build(self, package_info: PackageInfo, target: DeployTarget) -> PackageInfo:
        """构建并推送镜像到仓库"""
        # 确定镜像地址
        registry = target.extra.get("registry", "")
        namespace = target.extra.get("image_namespace", "agentengin")
        tag = target.extra.get("tag", "latest")
        
        if registry:
            image_name = f"{registry}/{namespace}/{package_info.name}:{tag}"
        else:
            image_name = f"{namespace}/{package_info.name}:{tag}"
        
        # 构建镜像
        build_cmd = [
            "docker", "build",
            "-t", image_name,
            "-f", package_info.dockerfile,
            package_info.build_dir
        ]
        
        result = subprocess.run(build_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"Docker build failed: {result.stderr}")
        
        # 推送镜像 (如果指定了 registry)
        if registry:
            push_cmd = ["docker", "push", image_name]
            result = subprocess.run(push_cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                raise RuntimeError(f"Docker push failed: {result.stderr}")
        
        package_info.image = image_name
        return package_info
    
    async def deploy(self, package_info: PackageInfo, target: DeployTarget) -> DeployResult:
        """部署到 K8s"""
        namespace = target.extra.get("namespace", "default")
        kubeconfig = target.extra.get("kubeconfig")
        
        # 读取并渲染 K8s 配置
        k8s_manifest = package_info.metadata.get("k8s_manifest")
        if not k8s_manifest:
            return DeployResult(
                status=DeployStatus.FAILED,
                message="K8s manifest not found"
            )
        
        config_content = Path(k8s_manifest).read_text()
        config_content = config_content.replace("{{NAME}}", package_info.name)
        config_content = config_content.replace("{{NAMESPACE}}", namespace)
        config_content = config_content.replace("{{IMAGE}}", package_info.image)
        config_content = config_content.replace("{{PORT}}", str(target.extra.get("port", 8000)))
        
        # 资源配置
        config_content = config_content.replace("{{CPU}}", target.resources.cpu)
        config_content = config_content.replace("{{MEMORY}}", target.resources.memory)
        config_content = config_content.replace("{{REPLICAS}}", str(target.scaling.min_replicas))
        
        # 写入临时文件
        final_manifest = Path(package_info.build_dir) / "k8s-deployment-final.yaml"
        final_manifest.write_text(config_content)
        
        # 应用配置
        cmd = ["kubectl", "apply", "-f", str(final_manifest)]
        if kubeconfig:
            cmd.extend(["--kubeconfig", kubeconfig])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode == 0:
            return DeployResult(
                status=DeployStatus.DEPLOYING,
                agent_id=package_info.name,
                agent_name=package_info.name,
                endpoint=f"http://{package_info.name}.{namespace}.svc.cluster.local:8000",
                message=result.stdout,
                metadata={"namespace": namespace}
            )
        else:
            return DeployResult(
                status=DeployStatus.FAILED,
                agent_name=package_info.name,
                message=result.stderr
            )
    
    async def get_status(self, agent_id: str, target: DeployTarget) -> DeployResult:
        """获取 K8s 部署状态"""
        namespace = target.extra.get("namespace", "default")
        kubeconfig = target.extra.get("kubeconfig")
        
        cmd = [
            "kubectl", "get", "deployment", agent_id,
            "-n", namespace,
            "-o", "jsonpath={.status.availableReplicas}/{.status.replicas}"
        ]
        if kubeconfig:
            cmd.extend(["--kubeconfig", kubeconfig])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            status_str = result.stdout.strip()
            if "/" in status_str:
                available, total = status_str.split("/")
                if available == total and available != "":
                    status = DeployStatus.RUNNING
                else:
                    status = DeployStatus.DEPLOYING
            else:
                status = DeployStatus.UNKNOWN
            
            return DeployResult(
                status=status,
                agent_id=agent_id,
                message=f"Replicas: {status_str}"
            )
        else:
            return DeployResult(
                status=DeployStatus.UNKNOWN,
                agent_id=agent_id,
                message="Deployment not found"
            )
    
    async def destroy(self, agent_id: str, target: DeployTarget) -> bool:
        """删除 K8s 部署"""
        namespace = target.extra.get("namespace", "default")
        kubeconfig = target.extra.get("kubeconfig")
        
        cmd = ["kubectl", "delete", "deployment,service", agent_id, "-n", namespace]
        if kubeconfig:
            cmd.extend(["--kubeconfig", kubeconfig])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    
    def _generate_k8s_manifest(self) -> str:
        return '''apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{NAME}}
  namespace: {{NAMESPACE}}
spec:
  replicas: {{REPLICAS}}
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
        image: {{IMAGE}}
        imagePullPolicy: Always
        ports:
        - containerPort: {{PORT}}
        resources:
          requests:
            cpu: {{CPU}}
            memory: {{MEMORY}}
          limits:
            cpu: {{CPU}}
            memory: {{MEMORY}}
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
