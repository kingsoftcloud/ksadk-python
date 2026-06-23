"""
MCP Builders - FastMCP 代码打包 / 容器构建

针对 MCP Server 进行定制：
- Code 模式: 依赖 fastmcp, uvicorn，entrypoint 使用 mcp.run(transport="http")
- Container 模式: 复用现有 Docker 登录/推送逻辑，但检测/打包走 FastMCP 专属链路
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import List

import click

from ksadk.builders.code_builder import CodeBuilder
from ksadk.builders.base import BuildResult
from ksadk.builders.container_builder import ContainerBuilder, ensure_docker_running
from ksadk.builders.requirements_utils import merge_requirement_lists, parse_requirements_text
from ksadk.detection.mcp_detector import MCPDetector, MCPDetectionResult


class MCPCodeBuilder(CodeBuilder):
    """MCP 代码构建器"""
    
    def __init__(self, project_dir: Path, config: dict = None):
        super().__init__(project_dir, config)
        self.build_dir = self.project_dir / ".agentengine" / "mcp_build"
        self.deps_dir = self.build_dir / "linux_deps"
        self._mcp_detection: MCPDetectionResult = None
    
    def build(self) -> BuildResult:
        """执行 MCP 构建"""
        self._load_dotenv()
        config = self._load_config()
        
        # 检测 MCP 项目
        detector = MCPDetector(str(self.project_dir))
        detection_result = detector.detect()
        self._mcp_detection = detection_result
        
        if not detection_result.is_valid:
            return BuildResult(
                success=False,
                error_message="未检测到 FastMCP 项目"
            )
        
        click.echo(f"📦 MCP Server: {click.style(detection_result.name, fg='green')}")
        if detection_result.tools:
            click.echo(f"   工具: {', '.join(detection_result.tools[:5])}")
            if len(detection_result.tools) > 5:
                click.echo(f"   ... 及其他 {len(detection_result.tools) - 5} 个")
        
        mcp_name = config.get('name', self.project_dir.name).replace('-', '_').replace('.', '_')
        
        # 创建构建目录
        self.build_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.build_dir / f"{mcp_name}.zip"
        
        # 检查是否需要重新构建
        no_cache = self.config.get("no_cache", False) if self.config else False
        if zip_path.exists() and not no_cache and not self._need_rebuild(zip_path, detection_result):
            incompatibles = self._scan_incompatible_binaries_in_zip(zip_path)
            if incompatibles:
                click.secho("\n⚠️ 检测到缓存 MCP 构建包含非 Linux 兼容关键二进制，自动重建...", fg='yellow')
                for item in incompatibles[:5]:
                    click.echo(f"   - {item}")
            else:
                self._save_input_fingerprint(zip_path, detection_result)
                zip_size = zip_path.stat().st_size / (1024 * 1024)
                click.secho(f"\n✅ 使用已有构建: {zip_path.name} ({zip_size:.2f} MB)", fg='green')
                return BuildResult(
                    success=True,
                    artifact_path=zip_path,
                    artifact_size=zip_path.stat().st_size,
                    metadata={
                        "mcp_name": mcp_name,
                        "mcp_variable": detection_result.mcp_variable,
                        "tools": detection_result.tools
                    }
                )
        
        # Step 1: 准备依赖
        click.echo("\n📋 Step 1/3: 准备依赖清单...")
        requirements_path = self._prepare_mcp_requirements(detection_result)
        
        # Step 2: 安装依赖（在 Mac 上安装后自动替换为 Linux 二进制，和 agent code 模式一致）
        click.echo("\n📦 Step 2/3: 安装依赖...")
        if self.deps_dir.exists():
            import shutil
            shutil.rmtree(self.deps_dir)
        self.deps_dir.mkdir(parents=True)
        
        if not self._install_dependencies(requirements_path):
            return BuildResult(
                success=False,
                error_message="依赖安装失败"
            )
        
        # Step 3: 打包
        click.echo("\n📦 Step 3/3: 打包 zip...")
        self._package_mcp_zip(zip_path, detection_result)
        self._save_input_fingerprint(zip_path, detection_result)
        
        zip_size = zip_path.stat().st_size
        click.echo(f"   zip 文件: {zip_path}")
        click.echo(f"   大小: {zip_size / (1024 * 1024):.2f} MB")
        
        return BuildResult(
            success=True,
            artifact_path=zip_path,
            artifact_size=zip_size,
            metadata={
                "mcp_name": mcp_name,
                "mcp_variable": detection_result.mcp_variable,
                "tools": detection_result.tools,
                "deps_dir": str(self.deps_dir)
            }
        )
    
    def _prepare_mcp_requirements(self, detection_result: MCPDetectionResult) -> Path:
        """准备 MCP 依赖"""
        base_deps = self._get_mcp_base_requirements()
        final_deps = list(base_deps)
        
        # 合并用户依赖
        user_requirements = self.project_dir / "requirements.txt"
        if user_requirements.exists():
            click.echo(f"   发现 requirements.txt，正在合并...")
            user_content = user_requirements.read_text()
            user_deps = parse_requirements_text(user_content)
            final_deps = merge_requirement_lists(final_deps, user_deps)
        else:
            click.echo(f"   使用默认 MCP 依赖")
        
        # 写入构建目录
        requirements_path = self.build_dir / "requirements.txt"
        requirements_path.write_text("\n".join(final_deps))
        
        click.echo(f"   共 {len(final_deps)} 个依赖包")
        
        return requirements_path
    
    def _get_mcp_base_requirements(self) -> List[str]:
        """MCP 基础依赖"""
        return [
            # FastMCP 核心
            "fastmcp>=2.0.0",
            # HTTP Server
            "uvicorn>=0.23.0",
            "httpx>=0.24.0",
            # 通用
            "python-dotenv>=1.0.0",
            "pydantic>=2.0.0",
        ]
    
    def _package_mcp_zip(self, zip_path: Path, detection_result: MCPDetectionResult) -> None:
        """打包 MCP zip"""
        import zipfile
        
        file_count = 0
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            # 添加项目文件
            for file_path in self._iter_project_files():
                arcname = file_path.relative_to(self.project_dir).as_posix()
                zf.write(file_path, arcname)
                file_count += 1
            
            # 添加依赖（已替换为 Linux 二进制）
            deps_count = 0
            for file_path in self.deps_dir.rglob('*'):
                if file_path.is_file():
                    arcname = str(file_path.relative_to(self.deps_dir))
                    zf.write(file_path, arcname)
                    deps_count += 1
            
            # 生成 MCP entrypoint
            entrypoint_content = self._generate_mcp_entrypoint(detection_result)
            zf.writestr("entrypoint.py", entrypoint_content)
        
        click.echo(f"   ✓ 打包完成: {file_count} 个项目文件 + {deps_count} 个依赖文件")
    
    def _generate_mcp_entrypoint(self, detection_result: MCPDetectionResult) -> str:
        """生成 MCP 入口脚本"""
        # 确定包名
        entry_point = detection_result.entry_point
        package_path = Path(detection_result.package_path)
        
        # 计算模块路径
        if "/" in entry_point:
            # 如果入口在子目录，如 my_mcp/server.py
            module_path = entry_point.replace("/", ".").replace(".py", "")
        else:
            # 根目录
            module_path = entry_point.replace(".py", "")
        
        mcp_variable = detection_result.mcp_variable
        
        return f'''"""
AgentEngine MCP 入口

FastMCP Server with HTTP Transport
"""

import sys
import os
import logging

# 日志配置
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)

logger = logging.getLogger("mcp_entrypoint")

# 路径设置
CODE_ROOT = os.environ.get("CODE_PATH", "/app/code")
sys.path.insert(0, CODE_ROOT)
os.chdir(CODE_ROOT)

logger.info("=" * 60)
logger.info("MCP Server 启动")
logger.info("=" * 60)
logger.info(f"CODE_ROOT: {{CODE_ROOT}}")

# 加载环境变量
try:
    from dotenv import load_dotenv
    env_file = os.path.join(CODE_ROOT, ".env")
    if os.path.exists(env_file):
        load_dotenv(env_file)
        logger.info("已加载 .env")
except ImportError:
    pass

# 导入 MCP 实例
from {module_path} import {mcp_variable}

logger.info(f"MCP 实例: {mcp_variable}")
logger.info(f"工具数量: {{len(getattr({mcp_variable}, '_tool_manager', {{}}).list_tools() if hasattr({mcp_variable}, '_tool_manager') else 'N/A')}}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0")
    
    logger.info(f"启动 HTTP Server: {{host}}:{{port}}")
    logger.info("MCP endpoint: /mcp")
    
    {mcp_variable}.run(
        transport="http",
        host=host,
        port=port
    )
'''


class MCPContainerBuilder(ContainerBuilder):
    """FastMCP Docker 镜像构建器。"""

    def __init__(self, project_dir: Path, config: dict = None,
                 tag: str = None, registry: str = None, no_cache: bool = False):
        super().__init__(project_dir, config=config, tag=tag, registry=registry, no_cache=no_cache)

    def build(self) -> BuildResult:
        """构建 MCP Docker 镜像。"""
        self._load_dotenv()
        config = self._load_config()

        detector = MCPDetector(str(self.project_dir))
        result = detector.detect()

        if not result.is_valid:
            return BuildResult(
                success=False,
                error_message="未检测到 FastMCP 项目"
            )

        click.echo(f"📦 MCP Server: {click.style(result.name, fg='green')}")

        image_name = config.get('name', self.project_dir.name).replace('-', '_').replace('.', '_')

        image_tag = self.tag
        if not image_tag:
            image_tag = config.get('version', '')
        if not image_tag:
            image_tag = config.get('image', {}).get('tag', 'latest')

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

        click.echo("\n📦 打包中...")
        try:
            package_info = self._package_mcp_project(result)
            click.echo("✅ 打包完成")
        except Exception as e:
            return BuildResult(
                success=False,
                error_message=f"打包失败: {e}"
            )

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
                artifact_path=None,
                metadata={
                    "image": full_image,
                    "framework": "mcp",
                    "build_dir": package_info.build_dir,
                    "mcp_variable": result.mcp_variable,
                    "tools": result.tools,
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

    def _package_mcp_project(self, detection_result: MCPDetectionResult):
        """复制项目并生成 FastMCP 容器运行所需文件。"""
        from ksadk.deployment.base import PackageInfo

        project_path = self.project_dir
        output_dir = project_path / ".agentengine" / "container_build"
        output_dir.mkdir(parents=True, exist_ok=True)

        for item in project_path.iterdir():
            if CodeBuilder._is_real_dotenv_file(item.name):
                continue
            if (
                (item.name.startswith('.') and item.name != '.env.example')
                or item.name in ('__pycache__', '.git', 'node_modules')
            ):
                continue
            dest = output_dir / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                shutil.copy2(item, dest)

        dockerfile_path = output_dir / "Dockerfile"
        dockerfile_path.write_text(self._generate_dockerfile(detection_result))

        requirements_path = output_dir / "requirements.txt"
        requirements_path.write_text(self._generate_mcp_requirements(project_path))

        entrypoint_path = output_dir / "entrypoint.py"
        entrypoint_path.write_text(self._generate_mcp_entrypoint(detection_result))

        return PackageInfo(
            name=detection_result.name or project_path.name,
            framework="mcp",
            build_dir=str(output_dir),
            project_dir=str(project_path),
            dockerfile=str(dockerfile_path),
            entry_point=detection_result.entry_point,
            metadata={
                "requirements": str(requirements_path),
                "entrypoint": str(entrypoint_path),
                "mcp_variable": detection_result.mcp_variable,
                "tools": detection_result.tools,
            }
        )

    def _generate_mcp_requirements(self, project_path: Path) -> str:
        """生成 MCP 容器 requirements.txt。"""
        base_deps = [
            "fastmcp>=2.0.0",
            "uvicorn>=0.23.0",
            "httpx>=0.24.0",
            "python-dotenv>=1.0.0",
            "pydantic>=2.0.0",
        ]

        user_requirements = project_path / "requirements.txt"
        if user_requirements.exists():
            user_content = user_requirements.read_text()
            user_deps = parse_requirements_text(user_content)
            base_deps = merge_requirement_lists(base_deps, user_deps)

        return "\n".join(base_deps)

    def _generate_mcp_entrypoint(self, detection_result: MCPDetectionResult) -> str:
        """生成 MCP 容器入口脚本。"""
        module_path = detection_result.entry_point.replace("/", ".").replace(".py", "")
        mcp_variable = detection_result.mcp_variable

        return f'''"""
AgentEngine MCP Container 入口
"""

import sys
import os
import logging

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)

logger = logging.getLogger("mcp_container_entrypoint")

sys.path.insert(0, "/app")
os.chdir("/app")

try:
    from dotenv import load_dotenv
    if os.path.exists("/app/.env"):
        load_dotenv("/app/.env")
        logger.info("已加载 .env 文件")
except ImportError:
    pass

from {module_path} import {mcp_variable}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"启动 MCP HTTP Server: {{host}}:{{port}}")
    {mcp_variable}.run(
        transport="http",
        host=host,
        port=port,
    )
'''
