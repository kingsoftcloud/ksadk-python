"""
Container Builder - Docker 镜像构建
"""

import subprocess
import shutil
import time
import platform
import asyncio
from pathlib import Path
from typing import Optional

import click

from ksadk.builders.base import BaseBuilder, BuildResult


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
    
    if platform.system() == "Darwin":
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
    
    def build(self) -> BuildResult:
        """构建 Docker 镜像"""
        from ksadk.detection import FrameworkDetector
        from ksadk.deployment import DeploymentManager
        
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
        image_tag = self.tag or config.get('image', {}).get('tag', 'latest')
        image_registry = self.registry or config.get('image', {}).get('registry', '')
        
        if image_registry:
            full_image = f"{image_registry}/{image_name}:{image_tag}"
        else:
            full_image = f"agentengine/{image_name}:{image_tag}"
        
        click.echo(f"🏷️  镜像名称: {full_image}")
        
        # 打包
        deployer = DeploymentManager.create("docker")
        
        click.echo("\n📦 打包中...")
        try:
            package_info = asyncio.run(deployer.package(str(self.project_dir), result))
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
        
        click.echo("\n🔨 构建 Docker 镜像...")
        try:
            cmd = ['docker', 'build', '-t', full_image]
            if self.no_cache:
                cmd.append('--no-cache')
            cmd.append(package_info.build_dir)
            
            subprocess.run(cmd, check=True)
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
            return BuildResult(
                success=False,
                error_message=f"镜像构建失败: {e}"
            )
    
    def push(self, image_name: str) -> bool:
        """推送镜像到仓库"""
        click.echo(f"\n📤 推送镜像...")
        try:
            subprocess.run(['docker', 'push', image_name], check=True)
            click.secho(f"✅ 镜像推送成功", fg='green')
            return True
        except subprocess.CalledProcessError as e:
            click.secho(f"❌ 镜像推送失败: {e}", fg='red')
            return False
