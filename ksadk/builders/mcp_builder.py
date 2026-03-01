"""
MCP Code Builder - FastMCP 代码打包

继承 CodeBuilder，针对 MCP Server 进行定制：
- 依赖: fastmcp, uvicorn
- entrypoint: 使用 mcp.run(transport="http")
"""

import os
from pathlib import Path
from typing import List

import click

from ksadk.builders.code_builder import CodeBuilder
from ksadk.builders.base import BuildResult
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
        if zip_path.exists() and not no_cache and not self._need_rebuild(zip_path):
            incompatibles = self._scan_incompatible_binaries_in_zip(zip_path)
            if incompatibles:
                click.secho("\n⚠️ 检测到缓存 MCP 构建包含非 Linux 兼容关键二进制，自动重建...", fg='yellow')
                for item in incompatibles[:5]:
                    click.echo(f"   - {item}")
            else:
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
            user_deps = [l.strip() for l in user_content.split('\n') if l.strip() and not l.startswith('#')]
            final_deps.extend(user_deps)
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
            for item in self.project_dir.iterdir():
                if item.name.startswith('.'):
                    if item.name != '.env':
                        continue
                
                if item.name in (
                    '__pycache__', 'node_modules', '.git', '.venv', 'venv', 'env',
                    'site-packages', 'dist-packages', 'lib', 'lib64'
                ):
                    continue
                
                if item.is_file():
                    zf.write(item, item.name)
                    file_count += 1
                elif item.is_dir():
                    for file_path in item.rglob('*'):
                        if '__pycache__' in str(file_path) or file_path.suffix == '.pyc':
                            continue
                        if file_path.is_file():
                            arcname = str(file_path.relative_to(self.project_dir))
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
