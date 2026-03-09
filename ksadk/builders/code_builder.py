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
import re
from pathlib import Path
from typing import List, Optional, Set

import click

from ksadk.builders.base import BaseBuilder, BuildResult


class CodeBuilder(BaseBuilder):
    """Code 模式构建器 - 打包 zip + 依赖"""
    
    def __init__(self, project_dir: Path, config: dict = None):
        super().__init__(project_dir, config)
        self.build_dir = self.project_dir / ".agentengine" / "code_build"
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
        
        click.echo(f"📦 框架: {click.style(detection_result.type.value, fg='green')}")
        click.echo(f"🤖 Agent: {click.style(detection_result.name, fg='blue')}")
        
        agent_name = config.get('name', self.project_dir.name).replace('-', '_').replace('.', '_')
        
        # 创建构建目录
        self.build_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.build_dir / f"{agent_name}.zip"
        
        # 检查是否需要重新构建
        no_cache = self.config.get("no_cache", False) if self.config else False
        if zip_path.exists() and not no_cache and not self._need_rebuild(zip_path):
            incompatibles = self._scan_incompatible_binaries_in_zip(zip_path)
            if incompatibles:
                click.secho("\n⚠️ 检测到缓存构建包含非 Linux 兼容关键二进制，自动重建...", fg='yellow')
                for item in incompatibles[:5]:
                    click.echo(f"   - {item}")
            else:
                zip_size = zip_path.stat().st_size / (1024 * 1024)
                click.secho(f"\n✅ 使用已有构建: {zip_path.name} ({zip_size:.2f} MB)", fg='green')
                click.echo("   (如需重新构建，请使用 --no-cache 或删除 .agentengine/code_build 目录)")
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
            "langfuse>=2.0.0",
        ]
        
        framework = detection_result.type.value
        if framework == "adk":
            deps += ["google-adk>=0.1.0", "litellm>=1.0.0"]
        elif framework in ("langchain", "langgraph", "deepagents"):
            # LangChain 生态统一依赖 (langchain 和 langgraph 经常混用)
            deps += [
                # LangChain 核心
                "langchain>=0.1.0",
                "langchain-openai>=0.1.0",
                "langchain-core>=0.1.0",
                # LangGraph (即使检测到 langchain，很多用户也会用 langgraph 构建工作流)
                "langgraph>=0.1.0",
                # MCP (Model Context Protocol) 支持
                "mcp>=1.1.0",
                "langchain-mcp-adapters>=0.0.1",
            ]
            if framework == "deepagents":
                deps += ["deepagents>=0.3.0"]
        
        return deps
    
    # 目标 Python 版本 (必须与容器运行时一致)
    TARGET_PYTHON_VERSION = "312"  # 容器中为 Python 3.12
    
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
            # pip install -t，尝试多个镜像源
            # 注意: 先正常安装所有依赖 (含纯 Python 包)，macOS 二进制在后续 _replace_platform_binaries() 中替换
            mirror_sources = [
                "https://mirrors.aliyun.com/pypi/simple",
                "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
                "https://mirrors.cloud.tencent.com/pypi/simple",
            ]
            
            result = None
            for mirror in mirror_sources:
                install_cmd = [
                    sys.executable, "-m", "pip", "install",
                    "-r", str(requirements_path),
                    "-t", str(self.deps_dir),
                    "-i", mirror,
                    "--disable-pip-version-check",
                    "--no-warn-script-location",
                    "--retries", "3",
                    "--timeout", "60",
                ]
                result = subprocess.run(install_cmd, capture_output=True, text=True, timeout=1200)
                if result.returncode == 0:
                    break
                click.echo(f'\r   源 {mirror} 失败，尝试下一个...', nl=False)
            
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
            
            # 替换非 Linux 平台的二进制文件 (macOS/Windows) 为 Linux 版本
            if sys.platform in ('darwin', 'win32'):
                self._replace_platform_binaries()
            
            # 二进制兼容性校验（避免把不可运行的包部署到 Linux Runtime）
            incompatibles = self._scan_incompatible_binaries_in_deps()
            if incompatibles:
                click.secho("\r   ✗ 检测到非 Linux 兼容关键二进制，构建终止", fg='red')
                for item in incompatibles[:10]:
                    click.echo(f"      - {item}")
                if any("tiktoken" in i for i in incompatibles):
                    click.echo("   提示: tiktoken 为 langchain-openai 必需；若替换失败可重试或检查网络/镜像")
                click.echo("   建议: 删除 .agentengine/*_build 后重新构建，或在 Linux 环境重新打包")
                return False
            
            deps_count = sum(1 for _ in self.deps_dir.rglob('*') if _.is_file())
            deps_size = sum(f.stat().st_size for f in self.deps_dir.rglob('*') if f.is_file()) / (1024 * 1024)
            click.secho(f"\r   ✓ 依赖安装完成: {deps_count} 个文件, {deps_size:.1f} MB", fg='green')
            
            return True
            
        except subprocess.TimeoutExpired:
            stop_spinner = True
            spinner_thread.join()
            click.secho("\r   ✗ 安装超时 (20分钟)", fg='red')
            return False
        except Exception as e:
            stop_spinner = True
            spinner_thread.join()
            click.secho(f"\r   ✗ 依赖安装失败: {e}", fg='red')
            return False
    
    def _replace_platform_binaries(self) -> None:
        """替换非 Linux 平台 (macOS/Windows) C 扩展为 Linux 版本"""
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
            'pydantic_core': 'pydantic-core',
            '_pydantic_core': 'pydantic-core',
            # tiktoken: langchain-openai 核心依赖, Rust 编译的 C 扩展
            'tiktoken': 'tiktoken',
            '_tiktoken': 'tiktoken',
            # 其他常见原生扩展
            'regex': 'regex',
            '_regex': 'regex',
            'multidict': 'multidict',
            'yarl': 'yarl',
            'aiohttp': 'aiohttp',
            'frozenlist': 'frozenlist',
            'charset_normalizer': 'charset-normalizer',
            'msgpack': 'msgpack',
            # Windows 特有
            'win32': 'pywin32',
            'win32com': 'pywin32',
        }
        
        # 找到所有二进制文件
        binary_files = []
        if sys.platform == 'darwin':
            binary_files = list(self.deps_dir.rglob('*.so')) + list(self.deps_dir.rglob('*.dylib'))
        elif sys.platform == 'win32':
            binary_files = list(self.deps_dir.rglob('*.pyd')) + list(self.deps_dir.rglob('*.dll'))
        
        if not binary_files:
            return
        
        # 提取需要替换的包名
        packages_to_replace: Set[str] = set()
        for bin_file in binary_files:
            rel_path = bin_file.relative_to(self.deps_dir)
            parts = rel_path.parts
            
            # 忽略 bin 目录下的 dll (通常是 runtime)
            if 'bin' in parts:
                continue
                
            if len(parts) > 1:
                detected_name = parts[0]
            else:
                detected_name = bin_file.name.split('.')[0]
            
            # 跳过特定文件夹
            if detected_name in ('__pycache__', 'bin', 'include', 'lib', 'Scripts'):
                continue
            
            if detected_name in MODULE_TO_PACKAGE:
                pkg_name = MODULE_TO_PACKAGE[detected_name]
                if pkg_name:
                    packages_to_replace.add(pkg_name)
            else:
                packages_to_replace.add(detected_name)
        
        if not packages_to_replace:
            return
        
        click.echo(f"\r   检测到 {len(binary_files)} 个二进制文件 ({sys.platform}), 替换 {len(packages_to_replace)} 个包为 Linux 版本")
        
        # 下载 Linux wheels
        wheels_dir = self.build_dir / "linux_wheels"
        if wheels_dir.exists():
            shutil.rmtree(wheels_dir)
        wheels_dir.mkdir(parents=True)
        
        replaced_count = 0
        mirror_sources = [
            "https://mirrors.aliyun.com/pypi/simple",
            "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
            "https://mirrors.cloud.tencent.com/pypi/simple",
            None,  # fallback to default index
        ]
        for pkg_name in packages_to_replace:
            # 提取确切的版本号以避免不兼容问题 (如 pydantic-core)
            target_version = ""
            search_name = pkg_name.replace('-', '_').lower()
            for info_dir in self.deps_dir.glob(f"{search_name}-*.dist-info"):
                version_str = info_dir.name[len(search_name)+1:-10]  # remove search_name + '-' and '.dist-info'
                target_version = f"=={version_str}"
                break
                
            pkg_with_version = f"{pkg_name}{target_version}"
            
            try:
                downloaded = False
                for mirror in mirror_sources:
                    download_cmd = [
                        sys.executable, "-m", "pip", "download",
                        pkg_with_version,
                        "-d", str(wheels_dir),
                        "--platform", "manylinux2014_x86_64",
                        "--platform", "manylinux_2_17_x86_64",
                        "--platform", "manylinux_2_28_x86_64",
                        "--platform", "musllinux_1_2_x86_64",
                        "--platform", "linux_x86_64",
                        "--python-version", self.TARGET_PYTHON_VERSION,
                        "--only-binary=:all:",
                        "--implementation", "cp",
                        "--no-deps",
                        "--quiet",
                        "--disable-pip-version-check",
                        "--retries", "2",
                        "--timeout", "30",
                    ]
                    if mirror:
                        download_cmd += ["-i", mirror]
                    result = subprocess.run(download_cmd, capture_output=True, text=True, timeout=90)
                    if result.returncode == 0:
                        downloaded = True
                        break
                if downloaded:
                    replaced_count += 1
                else:
                    failed_msg = result.stderr.strip().split('\n')[-1] if result and result.stderr else 'unknown'
                    click.secho(f"   ⚠ 替换失败: {pkg_with_version} ({failed_msg})", fg='yellow')
            except Exception as e:
                click.secho(f"   ⚠ 替换异常: {pkg_name} ({e})", fg='yellow')
        
        # 解压 wheel 覆盖到 deps_dir
        for wheel_file in wheels_dir.glob("*.whl"):
            try:
                wheel_name = wheel_file.name.split('-')[0].lower().replace('_', '-')
                
                # 删除旧的包目录
                for old_dir in self.deps_dir.iterdir():
                    if old_dir.is_dir() and old_dir.name.lower().replace('_', '-') == wheel_name:
                        shutil.rmtree(old_dir)
                        # 不要 break，可能由多个目录 (e.g. pydantic_core, pydantic_core-2.x.dist-info)
                
                # 删除根目录下的二进制文件
                for ext in ('*.so', '*.dylib', '*.pyd', '*.dll'):
                    for bin_file in self.deps_dir.glob(f"{wheel_name}*{ext[1:]}"):
                        try:
                            bin_file.unlink()
                        except:
                            pass
                    for bin_file in self.deps_dir.glob(f"{wheel_name.replace('-', '_')}*{ext[1:]}"):
                        try:
                            bin_file.unlink()
                        except:
                            pass
                
                # 解压新的 wheel
                with zipfile.ZipFile(wheel_file, 'r') as zf:
                    zf.extractall(self.deps_dir)
            except Exception:
                pass
        
        shutil.rmtree(wheels_dir, ignore_errors=True)
        
        # 清理所有残留的非 Linux 平台二进制文件
        # (wheel 解压后可能有旧的 darwin/win .so 文件未被覆盖)
        cleaned_count = 0
        for so_file in list(self.deps_dir.rglob('*.so')):
            name = so_file.name.lower()
            if 'darwin' in name or 'win' in name:
                try:
                    so_file.unlink()
                    cleaned_count += 1
                except Exception:
                    pass
        for dylib_file in list(self.deps_dir.rglob('*.dylib')):
            try:
                dylib_file.unlink()
                cleaned_count += 1
            except Exception:
                pass
        if cleaned_count > 0:
            click.echo(f"   ✓ 清理 {cleaned_count} 个残留平台二进制文件")
        
        click.echo(f"   ✓ 成功替换 {replaced_count}/{len(packages_to_replace)} 个包")

    def _is_linux_so(self, name: str) -> bool:
        lower = name.lower()
        return lower.endswith(".so") and "darwin" not in lower and "win" not in lower

    def _scan_incompatible_binaries_in_zip(self, zip_path: Path) -> List[str]:
        """扫描缓存 zip 中关键扩展模块是否缺失 Linux 版本。"""
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
            return self._detect_critical_binary_issues(names)
        except Exception:
            # 缓存损坏时交给后续重建流程处理
            return ["zip-read-failed"]

    def _scan_incompatible_binaries_in_deps(self) -> List[str]:
        """扫描 deps 目录中的关键扩展模块是否缺失 Linux 版本。"""
        names = []
        for file_path in self.deps_dir.rglob("*"):
            if file_path.is_file():
                names.append(file_path.relative_to(self.deps_dir).as_posix())
        return self._detect_critical_binary_issues(names)

    def _detect_critical_binary_issues(self, names: List[str]) -> List[str]:
        issues: List[str] = []
        
        # 定义需要检查的关键原生扩展模块
        # 格式: (描述, 正则模式)
        critical_modules = [
            # pydantic_core: pydantic v2 核心扩展，缺失会直接启动失败
            ("pydantic_core/_pydantic_core", r"pydantic_core/_pydantic_core.*\.(so|pyd)$"),
            # _cffi_backend: cryptography / cffi 常见依赖
            ("_cffi_backend", r"(^|/)_cffi_backend.*\.(so|pyd)$"),
            # tiktoken/_tiktoken: langchain-openai 核心依赖 (Rust 编译)
            ("tiktoken/_tiktoken", r"tiktoken/_tiktoken.*\.(so|pyd)$"),
        ]
        
        for module_name, pattern in critical_modules:
            matched_bins = [n for n in names if re.search(pattern, n)]
            if matched_bins:
                if not any(self._is_linux_so(n) for n in matched_bins):
                    issues.append(f"missing-linux:{module_name}")
        
        # 通用检查: 所有 .so 文件中不应包含 darwin/win 平台标识
        all_so_files = [n for n in names if n.endswith('.so')]
        darwin_so_count = sum(1 for n in all_so_files if 'darwin' in n.lower())
        if darwin_so_count > 0:
            issues.append(f"warning:found-{darwin_so_count}-darwin-so-files")
        
        return issues
    
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
                            arcname = file_path.relative_to(self.project_dir).as_posix()
                            zf.write(file_path, arcname)
                            file_count += 1
            
            # 添加依赖
            deps_count = 0
            for file_path in self.deps_dir.rglob('*'):
                if file_path.is_file():
                    arcname = file_path.relative_to(self.deps_dir).as_posix()
                    zf.write(file_path, arcname)
                    deps_count += 1
            
            # 添加 ksadk 源码
            import ksadk
            ksadk_src = Path(ksadk.__file__).parent
            ksadk_count = 0
            allowed_suffixes = {
                '.py',
                '.yaml',
                '.yml',
                '.json',
                '.jinja2',
                '.j2',
                '.txt',
                '.md',
                '.html',
                '.js',
                '.css',
                '.svg',
                '.ico',
                '.png',
                '.jpg',
                '.jpeg',
                '.gif',
                '.webp',
                '.woff',
                '.woff2',
                '.ttf',
                '.map',
            }
            
            for file_path in ksadk_src.rglob('*'):
                if not file_path.is_file():
                    continue
                if '__pycache__' in str(file_path):
                    continue
                if file_path.suffix not in allowed_suffixes:
                    if file_path.suffix in {'.pyc', '.pyd', '.so', '.dylib', '.dll', '.bin'}:
                        continue
                    continue
                
                arcname = "ksadk/" + file_path.relative_to(ksadk_src).as_posix()
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
import logging
from pathlib import Path

# ========== 日志配置 ==========
# 通过环境变量 LOG_LEVEL 控制日志级别 (DEBUG, INFO, WARNING, ERROR)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# 配置根日志
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,  # 覆盖已有配置
)

logger = logging.getLogger("entrypoint")
logger.info(f"日志级别: {{LOG_LEVEL}}")

# 配置第三方库日志级别
if LOG_LEVEL == "DEBUG":
    # DEBUG 模式下显示所有日志
    logging.getLogger("langchain").setLevel(logging.DEBUG)
    logging.getLogger("langgraph").setLevel(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("opentelemetry").setLevel(logging.DEBUG)
else:
    # 非 DEBUG 模式下减少第三方库噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

# LangChain 调试模式 (默认开启, 会打印完整的 prompt 和 LLM 调用信息)
# 设置 LANGCHAIN_VERBOSE=false 可关闭
if os.environ.get("LANGCHAIN_VERBOSE", "true").lower() not in ("false", "0"):
    try:
        from langchain.globals import set_verbose, set_debug
        set_verbose(True)
        set_debug(True)
        logger.info("LangChain 调试模式已启用")
    except ImportError:
        pass

# ========== 路径设置 ==========
CODE_ROOT = os.environ.get("CODE_PATH", "/app/code")
sys.path.insert(0, CODE_ROOT)
os.chdir(CODE_ROOT)

# 打印启动信息
logger.info("=" * 60)
logger.info("AgentEngine 启动")
logger.info("=" * 60)
logger.info(f"CODE_ROOT: {{CODE_ROOT}}")
logger.info(f"Python: {{sys.version}}")
logger.info(f"PYTHONPATH: {{os.environ.get('PYTHONPATH', 'N/A')}}")

# 打印关键环境变量 (隐藏敏感信息)
env_keys = ["AGENT_RUNTIME_NAME", "AGENT_RUNTIME_ID", "ACCOUNT_ID", "PORT", 
            "LANGFUSE_BASE_URL", "LANGCHAIN_TRACING_V2", "MODEL_NAME"]
for key in env_keys:
    value = os.environ.get(key)
    if value:
        # 隐藏敏感值
        if "KEY" in key or "SECRET" in key:
            value = value[:8] + "****" if len(value) > 8 else "****"
        logger.info(f"  {{key}}: {{value}}")

logger.info("=" * 60)

# ========== 加载 Agent ==========
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

logger.info(f"框架: {{detection_result.name}}")
logger.info(f"入口: {{detection_result.entry_point}}")

# 初始化 Tracing (如果配置了 Langfuse)
if os.environ.get("LANGFUSE_PUBLIC_KEY"):
    try:
        from ksadk.tracing import setup_tracing
        
        # 对于 LangChain 生态（LangGraph/DeepAgents），默认仅使用 Callback 以避免重复 Trace
        is_langchain = "{detection_result.type.name}" in ("LANGCHAIN", "LANGGRAPH", "DEEPAGENTS")
        
        setup_tracing(use_callback_only=is_langchain)
        logger.info(f"Tracing 已启用 (Langfuse, CallbackOnly={{is_langchain}})")
    except Exception as e:
        logger.warning(f"Tracing 初始化失败: {{e}}")

# 创建 Runner 并加载 Agent
logger.info("正在加载 Agent...")
runner = create_runner(detection_result, CODE_ROOT)
runner.load_agent()
set_runner(runner)
logger.info("Agent 加载成功!")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"启动 HTTP Server: 0.0.0.0:{{port}}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())
'''
