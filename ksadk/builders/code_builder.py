"""
Code Builder - zip 打包模式构建

构建流程:
1. 准备依赖清单 (requirements.txt)
2. 使用 pip 安装依赖 (自动替换 macOS 二进制为 Linux 版本)
3. 打包 zip (用户代码 + 依赖 + ksadk 源码 + entrypoint)
"""

import os
import sys
import shutil
import subprocess
import threading
import itertools
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Set

import click

from ksadk.builders.base import BaseBuilder, BuildResult


class CodeBuilder(BaseBuilder):
    """Code 模式构建器 - 打包 zip + 依赖"""
    
    def __init__(self, project_dir: Path, config: dict = None):
        super().__init__(project_dir, config)
        self.build_dir = self.project_dir / ".agentengin" / "code_build"
        self.deps_dir = self.build_dir / "linux_deps"
    
    def build(self) -> BuildResult:
        """执行 Code 模式构建"""
        from ksadk.detection import FrameworkDetector
        
        self._load_dotenv()
        config = self._load_config()
        
        # 检测框架
        detector = FrameworkDetector(str(self.project_dir))
        detection_result = detector.detect()
        
        if detection_result.type.value == "unknown":
            return BuildResult(
                success=False,
                error_message="未检测到支持的框架"
            )
        
        click.echo(f"📦 框架: {click.style(detection_result.name, fg='green')}")
        
        agent_name = config.get('name', self.project_dir.name).replace('-', '_').replace('.', '_')
        
        # 创建构建目录
        self.build_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.build_dir / f"{agent_name}.zip"
        
        # 检查是否需要重新构建
        if zip_path.exists() and not self._need_rebuild(zip_path):
            zip_size = zip_path.stat().st_size / (1024 * 1024)
            click.secho(f"\n✅ 使用已有构建: {zip_path.name} ({zip_size:.2f} MB)", fg='green')
            click.echo("   (如需重新构建，请删除 .agentengin/code_build 目录)")
            return BuildResult(
                success=True,
                artifact_path=zip_path,
                artifact_size=zip_path.stat().st_size,
                metadata={
                    "agent_name": agent_name,
                    "framework": detection_result.type.value
                }
            )
        
        # Step 1: 准备依赖
        click.echo("\n📋 Step 1/3: 准备依赖清单...")
        requirements_path = self._prepare_requirements(detection_result)
        
        # Step 2: 安装依赖
        click.echo("\n📦 Step 2/3: 安装依赖...")
        if self.deps_dir.exists():
            shutil.rmtree(self.deps_dir)
        self.deps_dir.mkdir(parents=True)
        
        if not self._install_dependencies(requirements_path):
            return BuildResult(
                success=False,
                error_message="依赖安装失败"
            )
        
        # Step 3: 打包 zip
        click.echo("\n📦 Step 3/3: 打包 zip...")
        self._package_zip(zip_path, detection_result)
        
        zip_size = zip_path.stat().st_size
        click.echo(f"   zip 文件: {zip_path}")
        click.echo(f"   大小: {zip_size / (1024 * 1024):.2f} MB")
        
        return BuildResult(
            success=True,
            artifact_path=zip_path,
            artifact_size=zip_size,
            metadata={
                "agent_name": agent_name,
                "framework": detection_result.type.value,
                "deps_dir": str(self.deps_dir)
            }
        )
    
    def _need_rebuild(self, zip_path: Path) -> bool:
        """检查是否需要重新构建"""
        zip_mtime = zip_path.stat().st_mtime
        
        for item in self.project_dir.iterdir():
            if item.name.startswith('.') or item.name in ('__pycache__', 'node_modules', '.git', '.venv', 'venv'):
                continue
            if item.is_file() and item.stat().st_mtime > zip_mtime:
                return True
            if item.is_dir():
                for file_path in item.rglob('*.py'):
                    if file_path.stat().st_mtime > zip_mtime:
                        return True
        return False
    
    def _prepare_requirements(self, detection_result) -> Path:
        """准备 requirements.txt"""
        base_deps = self._get_base_requirements(detection_result)
        final_deps = list(base_deps)
        
        # 合并用户依赖
        user_requirements = self.project_dir / "requirements.txt"
        if user_requirements.exists():
            click.echo(f"   发现 requirements.txt，正在合并...")
            user_content = user_requirements.read_text()
            user_deps = [l.strip() for l in user_content.split('\n') if l.strip() and not l.startswith('#')]
            final_deps.extend(user_deps)
        else:
            click.echo(f"   自动生成依赖清单")
        
        # 写入构建目录
        requirements_path = self.build_dir / "requirements.txt"
        requirements_path.write_text("\n".join(final_deps))
        
        click.echo(f"   共 {len(final_deps)} 个依赖包:")
        for dep in final_deps[:5]:
            click.echo(f"      • {dep}")
        if len(final_deps) > 5:
            click.echo(f"      ... 及其他 {len(final_deps) - 5} 个")
        
        return requirements_path
    
    def _get_base_requirements(self, detection_result) -> List[str]:
        """获取基础依赖列表"""
        deps = [
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
        ]
        
        framework = detection_result.type.value
        if framework == "adk":
            deps += ["google-adk>=0.1.0", "litellm>=1.0.0"]
        elif framework == "langchain":
            deps += ["langchain>=0.1.0", "langchain-openai>=0.1.0"]
        elif framework == "langgraph":
            deps += ["langgraph>=0.1.0", "langchain-openai>=0.1.0"]
        
        return deps
    
    def _install_dependencies(self, requirements_path: Path) -> bool:
        """安装依赖到 deps_dir"""
        stop_spinner = False
        
        def spinner():
            for c in itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']):
                if stop_spinner:
                    break
                click.echo(f'\r   {c} 正在安装依赖...', nl=False)
                time.sleep(0.1)
        
        spinner_thread = threading.Thread(target=spinner)
        spinner_thread.start()
        
        try:
            # pip install -t
            install_cmd = [
                sys.executable, "-m", "pip", "install",
                "-r", str(requirements_path),
                "-t", str(self.deps_dir),
                "-i", "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
                "--disable-pip-version-check",
                "--no-warn-script-location"
            ]
            
            result = subprocess.run(install_cmd, capture_output=True, text=True, timeout=600)
            
            stop_spinner = True
            spinner_thread.join()
            click.echo('\r                                        ', nl=False)
            
            if result.returncode != 0:
                click.secho(f"\r   ✗ 安装失败", fg='red')
                if result.stderr:
                    error_lines = [l for l in result.stderr.split('\n') if 'ERROR' in l.upper()][:3]
                    for line in error_lines:
                        click.echo(f"   {line}")
                return False
            
            # 替换 macOS 二进制为 Linux 版本
            if sys.platform == 'darwin':
                self._replace_macos_binaries()
            
            deps_count = sum(1 for _ in self.deps_dir.rglob('*') if _.is_file())
            deps_size = sum(f.stat().st_size for f in self.deps_dir.rglob('*') if f.is_file()) / (1024 * 1024)
            click.secho(f"\r   ✓ 依赖安装完成: {deps_count} 个文件, {deps_size:.1f} MB", fg='green')
            
            return True
            
        except subprocess.TimeoutExpired:
            stop_spinner = True
            spinner_thread.join()
            click.secho("\r   ✗ 安装超时 (10分钟)", fg='red')
            return False
        except Exception as e:
            stop_spinner = True
            spinner_thread.join()
            click.secho(f"\r   ✗ 依赖安装失败: {e}", fg='red')
            return False
    
    def _replace_macos_binaries(self) -> None:
        """替换 macOS C 扩展为 Linux 版本"""
        # 模块名到 pip 包名的映射
        MODULE_TO_PACKAGE = {
            '_cffi_backend': 'cffi',
            'yaml': 'pyyaml',
            '_yaml': 'pyyaml',
            'rpds': 'rpds-py',
            'PIL': 'pillow',
            'cv2': 'opencv-python',
            'sklearn': 'scikit-learn',
            '_watchdog_fsevents': 'watchdog',
            'google': None,  # 跳过命名空间包
            'grpc': 'grpcio',
            '_grpc': 'grpcio',
            'uuid_utils': 'uuid-utils',
        }
        
        # 找到所有 macOS .so 文件
        darwin_so_files = list(self.deps_dir.rglob('*.so')) + list(self.deps_dir.rglob('*.dylib'))
        
        if not darwin_so_files:
            return
        
        # 提取需要替换的包名
        packages_to_replace: Set[str] = set()
        for so_file in darwin_so_files:
            rel_path = so_file.relative_to(self.deps_dir)
            parts = rel_path.parts
            if len(parts) > 1:
                detected_name = parts[0]
            else:
                detected_name = so_file.name.split('.')[0]
            
            if detected_name in MODULE_TO_PACKAGE:
                pkg_name = MODULE_TO_PACKAGE[detected_name]
                if pkg_name:
                    packages_to_replace.add(pkg_name)
            else:
                packages_to_replace.add(detected_name)
        
        if not packages_to_replace:
            return
        
        click.echo(f"\r   检测到 {len(darwin_so_files)} 个 macOS .so 文件, 替换 {len(packages_to_replace)} 个包为 Linux 版本")
        
        # 下载 Linux wheels
        wheels_dir = self.build_dir / "linux_wheels"
        if wheels_dir.exists():
            shutil.rmtree(wheels_dir)
        wheels_dir.mkdir(parents=True)
        
        replaced_count = 0
        for pkg_name in packages_to_replace:
            try:
                download_cmd = [
                    "pip", "download",
                    pkg_name,
                    "-d", str(wheels_dir),
                    "--platform", "manylinux2014_x86_64",
                    "--platform", "manylinux_2_17_x86_64",
                    "--python-version", "312",
                    "--only-binary=:all:",
                    "--no-deps",
                    "--quiet"
                ]
                result = subprocess.run(download_cmd, capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    replaced_count += 1
            except Exception:
                pass
        
        # 解压 wheel 覆盖到 deps_dir
        for wheel_file in wheels_dir.glob("*.whl"):
            try:
                wheel_name = wheel_file.name.split('-')[0].lower().replace('_', '-')
                
                # 删除旧的包目录
                for old_dir in self.deps_dir.iterdir():
                    if old_dir.is_dir() and old_dir.name.lower().replace('_', '-') == wheel_name:
                        shutil.rmtree(old_dir)
                        break
                
                # 删除根目录下的 .so 文件
                for so_file in self.deps_dir.glob(f"{wheel_name}*.so"):
                    so_file.unlink()
                for so_file in self.deps_dir.glob(f"{wheel_name.replace('-', '_')}*.so"):
                    so_file.unlink()
                
                # 解压新的 wheel
                with zipfile.ZipFile(wheel_file, 'r') as zf:
                    zf.extractall(self.deps_dir)
            except Exception:
                pass
        
        shutil.rmtree(wheels_dir, ignore_errors=True)
        click.echo(f"   ✓ 成功替换 {replaced_count}/{len(packages_to_replace)} 个包")
    
    def _package_zip(self, zip_path: Path, detection_result) -> None:
        """打包 zip 文件"""
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
            
            # 添加依赖
            deps_count = 0
            for file_path in self.deps_dir.rglob('*'):
                if file_path.is_file():
                    arcname = str(file_path.relative_to(self.deps_dir))
                    zf.write(file_path, arcname)
                    deps_count += 1
            
            # 添加 ksadk 源码
            import ksadk
            ksadk_src = Path(ksadk.__file__).parent
            ksadk_count = 0
            allowed_suffixes = {'.py', '.yaml', '.yml', '.json', '.jinja2', '.j2', '.txt', '.md', '.html'}
            
            for file_path in ksadk_src.rglob('*'):
                if not file_path.is_file():
                    continue
                if '__pycache__' in str(file_path):
                    continue
                if file_path.suffix not in allowed_suffixes:
                    if file_path.suffix in {'.pyc', '.pyd', '.so', '.dylib', '.dll', '.bin'}:
                        continue
                    continue
                
                arcname = "ksadk/" + str(file_path.relative_to(ksadk_src))
                zf.write(file_path, arcname)
                ksadk_count += 1
            
            click.echo(f"   ✓ 打包 ksadk 源码: {ksadk_count} 个文件")
            
            # 添加 entrypoint
            entrypoint_content = self._generate_entrypoint(detection_result)
            zf.writestr("entrypoint.py", entrypoint_content)
        
        click.echo(f"   ✓ 打包完成: {file_count} 个项目文件 + {deps_count} 个依赖文件")
    
    def _generate_entrypoint(self, detection_result) -> str:
        """生成 entrypoint.py"""
        package_name = Path(detection_result.package_path).name
        return f'''"""
AgentEngine Code 模式入口

zip 包结构:
- entrypoint.py (本文件)
- {package_name}/ (Agent 代码)
- ksadk/ (ksadk 源码)
- fastapi/, uvicorn/, pydantic/ 等 (Linux 版依赖)
"""

import sys
import os
from pathlib import Path

# 添加代码路径
CODE_ROOT = os.environ.get("CODE_PATH", "/app/code")
sys.path.insert(0, CODE_ROOT)
os.chdir(CODE_ROOT)

# 加载环境变量
from ksadk.configs import setup_environment
setup_environment(Path(CODE_ROOT))

from ksadk.runners import create_runner
from ksadk.detection import DetectionResult, FrameworkType
from ksadk.server import app, set_runner
import uvicorn

# 检测结果 (构建时固化)
detection_result = DetectionResult(
    type=FrameworkType.{detection_result.type.name},
    name="{detection_result.name}",
    entry_point="{detection_result.entry_point}",
    package_path=os.path.join(CODE_ROOT, "{package_name}"),
    agent_variable="{detection_result.agent_variable}"
)

# 创建 Runner 并加载 Agent
runner = create_runner(detection_result, CODE_ROOT)
runner.load_agent()
set_runner(runner)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
'''
