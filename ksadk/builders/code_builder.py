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
import time
import zipfile
import re
import json
import hashlib
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click

from ksadk.builders.base import BaseBuilder, BuildResult
from ksadk.builders.framework_requirements import (
    FASTAPI_REQUIREMENT,
    requirements_for_framework,
)
from ksadk.builders.requirements_utils import (
    exclude_requirement_names,
    merge_requirement_lists,
    parse_requirements_text,
)


class CodeBuilder(BaseBuilder):
    """Code 模式构建器 - 打包 zip + 依赖"""

    INPUT_FINGERPRINT_VERSION = 1
    DEPENDENCY_FINGERPRINT_VERSION = 1
    TARGET_INSTALL_PLATFORMS = (
        "manylinux2014_x86_64",
        "manylinux_2_17_x86_64",
        "manylinux_2_28_x86_64",
        "musllinux_1_2_x86_64",
        "linux_x86_64",
    )
    PIP_INDEX_FALLBACKS = (
        "https://pypi.org/simple",
        "https://mirrors.aliyun.com/pypi/simple",
        "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
        "https://mirrors.cloud.tencent.com/pypi/simple",
    )
    IGNORED_ROOT_NAMES = {
        "__pycache__",
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "env",
        "site-packages",
        "dist-packages",
        "lib",
        "lib64",
    }
    IGNORED_DIR_NAMES = {
        "__pycache__",
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "env",
        "site-packages",
        "dist-packages",
        "lib",
        "lib64",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    IGNORED_FILE_NAMES = {".DS_Store"}
    KSADK_ALLOWED_SUFFIXES = {
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
    BUNDLED_KSADK_CORE_RUNTIME_REQUIREMENTS = (
        "a2a-sdk>=0.3.22",
        "httpx-sse>=0.4.0",
        "sse-starlette>=2.1.0",
        "python-multipart>=0.0.9,<1.0.0",
        "requests>=2.28.0",
        "requests-aws4auth>=1.2.0",
        "kingsoftcloud-sdk-python>=1.5.8.90",
        "cryptography>=44.0.0",
        "websockets>=12.0,<16.0",
        "qrcode>=7.4.0",
    )
    BUNDLED_KSADK_MCP_RUNTIME_REQUIREMENTS = (
        "mcp>=1.1.0",
        "langchain-mcp-adapters>=0.0.1",
    )
    BUNDLED_KSADK_POSTGRES_SESSION_REQUIREMENTS = (
        "asyncpg>=0.30.0,<1.0.0",
    )
    BUNDLED_KSADK_ATTACHMENT_RUNTIME_REQUIREMENTS = (
        "pypdf>=6.0.0",
        "beautifulsoup4>=4.12.0",
    )
    BUNDLED_KSADK_ATTACHMENT_OCR_RUNTIME_REQUIREMENTS = (
        "rapidocr-onnxruntime>=1.2.0",
    )
    BUNDLED_KSADK_RUNTIME_REQUIREMENTS = (
        *BUNDLED_KSADK_CORE_RUNTIME_REQUIREMENTS,
        *BUNDLED_KSADK_ATTACHMENT_RUNTIME_REQUIREMENTS,
    )
    INSTALL_PROGRESS_BAR_WIDTH = 24
    INSTALL_PROGRESS_EVENT_MILESTONES = frozenset({1, 5, 10})
    INSTALL_DOWNLOAD_PROGRESS_MAX = 84
    PACKAGE_PROGRESS_BAR_WIDTH = 24
    PACKAGE_PROGRESS_LOG_MILESTONES = (25, 50, 75, 100)
    PIP_INDEX_CACHE_VERSION = 1
    PIP_INDEX_CACHE_TTL_SECONDS = 6 * 60 * 60
    PIP_INDEX_PROBE_TIMEOUT_SECONDS = 1.5
    PIP_INSTALL_TIMEOUT_SECONDS = 45 * 60
    CONTAINER_SUGGESTION_RAW_THRESHOLD_BYTES = 500 * 1024 * 1024
    CONTAINER_SUGGESTION_ZIP_THRESHOLD_BYTES = 300 * 1024 * 1024
    
    def __init__(self, project_dir: Path, config: dict = None):
        super().__init__(project_dir, config)
        self.build_dir = self.project_dir / ".agentengine" / "code_build"
        self.deps_dir = self.build_dir / "linux_deps"
        self.pip_cache_dir = self.build_dir / "pip_cache"
        self._install_progress_width = 0
        self._install_progress_percent = 0
        self._install_progress_stage_name = ""
        self._install_progress_summary_text = ""
        self._install_progress_last_line = ""
        self._install_progress_event_counts: dict[str, int] = {}
        self._install_progress_started_at = time.monotonic()
        self._package_progress_width = 0
        self._package_progress_last_line = ""
        self._package_progress_last_percent_by_label: dict[str, int] = {}
        self._package_progress_logged_milestone_by_label: dict[str, int] = {}
        self._pip_index_candidates_cache: Optional[list[Optional[str]]] = None
        self._pip_index_selection_summary = ""
    
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
        repackage = self.config.get("repackage", False) if self.config else False
        rebuild_needed, rebuild_reason = self._rebuild_decision(
            zip_path,
            detection_result,
            no_cache=no_cache,
            repackage=repackage,
        )
        if zip_path.exists() and not rebuild_needed:
            incompatibles = self._scan_incompatible_binaries_in_zip(zip_path)
            if incompatibles:
                click.secho("\n⚠️ 检测到缓存构建包含非 Linux 兼容关键二进制，自动重建...", fg='yellow')
                for item in incompatibles[:5]:
                    click.echo(f"   - {item}")
                rebuild_reason = "缓存 zip 存在 Linux 兼容性问题"
            else:
                self._save_input_fingerprint(zip_path, detection_result)
                zip_size = zip_path.stat().st_size / (1024 * 1024)
                click.secho(f"\n✅ 使用已有构建: {zip_path.name} ({zip_size:.2f} MB)", fg='green')
                click.echo("   (如需只重新打包当前代码/runtime，请使用 --repackage；如需重装依赖，请使用 --no-cache)")
                return BuildResult(
                    success=True,
                    artifact_path=zip_path,
                    artifact_size=zip_path.stat().st_size,
                    metadata={
                        "agent_name": agent_name,
                        "framework": detection_result.type.value
                    }
                )
        elif rebuild_reason:
            click.echo(f"   重新打包原因: {rebuild_reason}")
        
        # Step 1: 准备依赖
        click.echo("\n📋 Step 1/3: 准备依赖清单...")
        requirements_path = self._prepare_requirements(detection_result)
        
        # Step 2: 安装依赖
        click.echo("\n📦 Step 2/3: 安装依赖...")
        reuse_dependencies, reuse_reason = self._can_reuse_dependency_cache(
            requirements_path,
            no_cache=no_cache,
        )
        if reuse_dependencies:
            stats = self._current_dependency_stats()
            click.secho(
                f"   ✓ 复用 Linux 依赖缓存: {stats['file_count']} 个文件, {stats['size_mb']:.1f} MB",
                fg="green",
            )
            click.echo(f"   {reuse_reason}")
        else:
            if reuse_reason:
                click.echo(f"   {reuse_reason}")
            self._clear_dependency_cache()
            self.deps_dir.mkdir(parents=True, exist_ok=True)
            
            if not self._install_dependencies(requirements_path):
                return BuildResult(
                    success=False,
                    error_message="依赖安装失败"
                )
            self._save_dependency_fingerprint(requirements_path)
        
        # Step 3: 打包 zip
        click.echo("\n📦 Step 3/3: 打包 zip...")
        package_started_at = time.monotonic()
        self._package_zip(zip_path, detection_result)
        click.echo(f"   ✓ 打包耗时: {self._format_elapsed(package_started_at)}")
        self._save_input_fingerprint(zip_path, detection_result)
        
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

    def _rebuild_decision(
        self,
        zip_path: Path,
        detection_result,
        *,
        no_cache: bool,
        repackage: bool,
    ) -> tuple[bool, str]:
        if no_cache:
            return True, "--no-cache 已开启，强制重新打包"
        if repackage:
            return True, "--repackage 已开启，复用依赖并重新打包当前代码/runtime"
        if not zip_path.exists():
            return True, "首次构建"

        previous = self._load_input_fingerprint(zip_path)
        if not previous:
            return self._need_rebuild_from_mtime(zip_path), "旧缓存缺少输入指纹，按文件时间判断"

        current = self._build_input_fingerprint(detection_result)
        if previous.get("fingerprint") == current["fingerprint"]:
            return False, ""
        return True, self._classify_rebuild_reason(previous, current)

    def _classify_rebuild_reason(self, previous: dict, current: dict) -> str:
        previous_files = set(previous.get("files") or [])
        current_files = set(current.get("files") or [])
        changed = previous_files.symmetric_difference(current_files)
        if not changed and previous.get("fingerprint") != current.get("fingerprint"):
            previous_digests = previous.get("file_digests") or {}
            current_digests = current.get("file_digests") or {}
            changed = {
                name
                for name in set(previous_digests) | set(current_digests)
                if previous_digests.get(name) != current_digests.get(name)
            }
        if any(str(name).startswith(("ksadk/", "ksadk_runtime_common/")) for name in changed):
            return "ksadk runtime 变更"
        if changed:
            return "业务代码或项目文件变更"
        return "构建输入指纹变化"
    
    def _need_rebuild(self, zip_path: Path, detection_result) -> bool:
        """检查是否需要重新构建。优先使用输入内容指纹，缺失时回退到 mtime。"""
        manifest = self._load_input_fingerprint(zip_path)
        if manifest:
            current_fingerprint = self._build_input_fingerprint(detection_result)
            return manifest.get("fingerprint") != current_fingerprint["fingerprint"]
        return self._need_rebuild_from_mtime(zip_path)

    def _need_rebuild_from_mtime(self, zip_path: Path) -> bool:
        """兼容旧缓存：当没有指纹文件时，回退到 mtime 判断。"""
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

    def _fingerprint_manifest_path(self, zip_path: Path) -> Path:
        return zip_path.with_suffix(".inputs.json")

    def _dependency_fingerprint_manifest_path(self) -> Path:
        return self.build_dir / "linux_deps.inputs.json"

    def _load_input_fingerprint(self, zip_path: Path) -> Optional[dict]:
        manifest_path = self._fingerprint_manifest_path(zip_path)
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != self.INPUT_FINGERPRINT_VERSION:
                return None
            return data
        except Exception:
            return None

    def _save_input_fingerprint(self, zip_path: Path, detection_result) -> None:
        manifest_path = self._fingerprint_manifest_path(zip_path)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_input_fingerprint(detection_result)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def _load_dependency_fingerprint(self) -> Optional[dict]:
        manifest_path = self._dependency_fingerprint_manifest_path()
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != self.DEPENDENCY_FINGERPRINT_VERSION:
                return None
            return data
        except Exception:
            return None

    def _save_dependency_fingerprint(self, requirements_path: Path) -> None:
        manifest_path = self._dependency_fingerprint_manifest_path()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_dependency_fingerprint(requirements_path)
        payload.update(self._current_dependency_stats())
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

    def _clear_dependency_cache(self) -> None:
        if self.deps_dir.exists():
            shutil.rmtree(self.deps_dir)
        manifest_path = self._dependency_fingerprint_manifest_path()
        if manifest_path.exists():
            manifest_path.unlink()

    def _pip_install_timeout_seconds(self) -> int:
        configured = os.getenv("KSADK_BUILD_PIP_INSTALL_TIMEOUT_SECONDS")
        if configured:
            try:
                value = int(configured)
                if value > 0:
                    return value
            except ValueError:
                pass
        return self.PIP_INSTALL_TIMEOUT_SECONDS

    def _attachment_ocr_runtime_enabled(self) -> bool:
        value = os.getenv("KSADK_BUILD_ENABLE_ATTACHMENT_OCR", "")
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def _bundled_runtime_requirements(self) -> tuple[str, ...]:
        requirements = list(self.BUNDLED_KSADK_RUNTIME_REQUIREMENTS)
        if self._attachment_ocr_runtime_enabled():
            requirements.extend(self.BUNDLED_KSADK_ATTACHMENT_OCR_RUNTIME_REQUIREMENTS)
        if self._mcp_runtime_enabled():
            requirements.extend(self.BUNDLED_KSADK_MCP_RUNTIME_REQUIREMENTS)
        if self._postgres_session_runtime_enabled():
            requirements.extend(self.BUNDLED_KSADK_POSTGRES_SESSION_REQUIREMENTS)
        return tuple(requirements)

    def _mcp_runtime_enabled(self) -> bool:
        if self._env_flag_enabled("KSADK_BUILD_ENABLE_MCP"):
            return True
        if self._project_env_has_configured_value("KSADK_MCP_SERVERS"):
            return True
        if self._project_env_value("KSADK_ENABLE_MCP_TOOLS").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        return self._project_imports_any({"mcp", "langchain_mcp_adapters"})

    def _postgres_session_runtime_enabled(self) -> bool:
        if self._env_flag_enabled("KSADK_BUILD_ENABLE_POSTGRES_SESSION"):
            return True
        backend = (
            self._project_env_value("KSADK_SESSION_BACKEND")
            or self._project_env_value("AGENTENGINE_SESSION_BACKEND")
            or self._project_env_value("KSADK_STM_BACKEND")
        )
        if backend.strip().lower() == "postgres":
            return True
        dsn = (
            self._project_env_value("KSADK_SESSION_DSN")
            or self._project_env_value("KSADK_STM_URL")
            or self._project_env_value("KSADK_STM_DB_URL")
        )
        return self._looks_like_postgres_dsn(dsn)

    def _env_flag_enabled(self, name: str) -> bool:
        return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}

    def _project_env_has_configured_value(self, name: str) -> bool:
        value = (os.getenv(name) or self._project_env_value(name)).strip()
        if not value:
            return False
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value.strip("'\"").lower() not in {"", "[]", "{}", "null", "none"}
        return bool(parsed)

    def _project_env_value(self, name: str) -> str:
        value = os.getenv(name)
        if value:
            return value
        for env_file in (self.project_dir / ".env", self.project_dir / "agentengine.env"):
            if not env_file.is_file():
                continue
            try:
                for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                    stripped = raw_line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    key, raw_value = stripped.split("=", 1)
                    if key.strip() == name:
                        return raw_value.strip().strip("'\"")
            except OSError:
                continue
        return ""

    def _looks_like_postgres_dsn(self, value: str) -> bool:
        normalized = (value or "").strip().lower()
        return normalized.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://"))

    def _project_imports_any(self, module_names: set[str]) -> bool:
        for py_file in self._iter_project_py_files():
            if self._should_skip_project_file(py_file):
                continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".", 1)[0]
                        if root in module_names:
                            return True
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".", 1)[0]
                    if root in module_names:
                        return True
        return False

    def _iter_project_py_files(self):
        for path in self._iter_project_files():
            if path.suffix == ".py":
                yield path

    def _build_dependency_fingerprint(self, requirements_path: Path) -> dict:
        digest = hashlib.sha256()
        requirements_text = requirements_path.read_text(encoding="utf-8")

        digest.update(f"deps-fingerprint-version:{self.DEPENDENCY_FINGERPRINT_VERSION}\n".encode("utf-8"))
        digest.update(f"builder-platform:{sys.platform}\n".encode("utf-8"))
        digest.update(
            f"builder-python:{sys.version_info.major}.{sys.version_info.minor}\n".encode("utf-8")
        )
        digest.update(f"target-python:{self.TARGET_PYTHON_VERSION}\n".encode("utf-8"))
        digest.update(f"target-platforms:{','.join(self.TARGET_INSTALL_PLATFORMS)}\n".encode("utf-8"))
        digest.update(requirements_text.encode("utf-8"))

        requirements = [line for line in requirements_text.splitlines() if line.strip()]
        return {
            "version": self.DEPENDENCY_FINGERPRINT_VERSION,
            "fingerprint": digest.hexdigest(),
            "requirements": requirements,
        }

    def _current_dependency_stats(self) -> dict:
        file_count = 0
        size_bytes = 0
        if self.deps_dir.exists():
            for file_path in self.deps_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                file_count += 1
                size_bytes += file_path.stat().st_size
        return {
            "file_count": file_count,
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
        }

    def _can_reuse_dependency_cache(
        self,
        requirements_path: Path,
        *,
        no_cache: bool = False,
    ) -> tuple[bool, str]:
        if no_cache:
            return False, "--no-cache 已开启，重新安装 Linux 依赖"

        if not self.deps_dir.exists():
            return False, "未发现现成 Linux 依赖缓存，重新安装"

        manifest = self._load_dependency_fingerprint()
        if manifest is None:
            return False, "缺少依赖缓存指纹，重新安装"

        current_fingerprint = self._build_dependency_fingerprint(requirements_path)
        if manifest.get("fingerprint") != current_fingerprint["fingerprint"]:
            return False, "依赖清单发生变化，重新安装"

        stats = self._current_dependency_stats()
        if stats["file_count"] == 0:
            return False, "依赖缓存为空，重新安装"

        incompatibles = self._scan_incompatible_binaries_in_deps()
        if incompatibles:
            preview = ", ".join(incompatibles[:3])
            return False, f"依赖缓存存在兼容性问题 ({preview})，重新安装"

        return True, "requirements 未变化，跳过依赖重装"

    def _build_input_fingerprint(self, detection_result) -> dict:
        digest = hashlib.sha256()
        files = []
        file_digests = {}

        digest.update(f"fingerprint-version:{self.INPUT_FINGERPRINT_VERSION}\n".encode("utf-8"))
        digest.update(f"framework:{detection_result.type.value}\n".encode("utf-8"))
        digest.update(f"entry:{getattr(detection_result, 'entry_point', '')}\n".encode("utf-8"))
        digest.update(f"target-python:{self.TARGET_PYTHON_VERSION}\n".encode("utf-8"))

        for dep in self._build_requirements_list(detection_result):
            digest.update(dep.encode("utf-8"))
            digest.update(b"\n")

        for file_path in self._iter_project_files():
            relative = file_path.relative_to(self.project_dir).as_posix()
            files.append(relative)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            file_digest = hashlib.sha256()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    file_digest.update(chunk)
            digest.update(b"\0")
            file_digests[relative] = file_digest.hexdigest()

        for package_name, relative, file_path in self._iter_bundled_source_files():
            fingerprint_name = f"{package_name}/{relative}"
            files.append(fingerprint_name)
            digest.update(fingerprint_name.encode("utf-8"))
            digest.update(b"\0")
            file_digest = hashlib.sha256()
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    file_digest.update(chunk)
            digest.update(b"\0")
            file_digests[fingerprint_name] = file_digest.hexdigest()

        return {
            "version": self.INPUT_FINGERPRINT_VERSION,
            "fingerprint": digest.hexdigest(),
            "files": files,
            "file_digests": file_digests,
        }

    def _iter_bundled_source_files(self):
        import ksadk
        import ksadk_runtime_common

        yield from self._iter_bundled_source_package("ksadk", Path(ksadk.__file__).resolve().parent)
        yield from self._iter_bundled_source_package(
            "ksadk_runtime_common",
            Path(ksadk_runtime_common.__file__).resolve().parent,
        )

    def _iter_bundled_source_package(self, package_name: str, package_root: Path):
        for file_path in sorted(package_root.rglob('*')):
            if not file_path.is_file():
                continue
            relative_path = file_path.relative_to(package_root)
            if package_name == "ksadk" and self._should_skip_ksadk_relative_path(relative_path):
                continue
            if '__pycache__' in file_path.parts:
                continue
            suffix = file_path.suffix.lower()
            if suffix not in self.KSADK_ALLOWED_SUFFIXES:
                continue
            yield package_name, relative_path.as_posix(), file_path

    def _should_skip_ksadk_relative_path(self, relative_path: Path) -> bool:
        parts = relative_path.parts
        return len(parts) >= 2 and parts[0] == "server" and parts[1] == "web-ui"

    def _iter_project_files(self):
        for item in sorted(self.project_dir.iterdir(), key=lambda p: p.name):
            if self._should_skip_root_path(item):
                continue
            if item.is_file():
                yield item
            elif item.is_dir():
                yield from self._walk_project_dir(item)

    def _walk_project_dir(self, root_dir: Path):
        for current_root, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = sorted(d for d in dirnames if not self._should_skip_dir_name(d))
            for filename in sorted(filenames):
                file_path = Path(current_root) / filename
                if self._should_skip_project_file(file_path):
                    continue
                yield file_path

    def _should_skip_root_path(self, path: Path) -> bool:
        if path.name.startswith(".") and path.name != ".env":
            return True
        if path.name in self.IGNORED_ROOT_NAMES:
            return True
        if path.is_dir() and self._should_skip_dir_name(path.name):
            return True
        if path.is_file() and self._should_skip_project_file(path):
            return True
        return False

    def _should_skip_dir_name(self, dir_name: str) -> bool:
        return dir_name in self.IGNORED_DIR_NAMES

    def _should_skip_project_file(self, file_path: Path) -> bool:
        if file_path.name in self.IGNORED_FILE_NAMES:
            return True
        if file_path.suffix == ".pyc":
            return True
        if "__pycache__" in file_path.parts:
            return True
        return False
    
    def _prepare_requirements(self, detection_result) -> Path:
        """准备 requirements.txt"""
        final_deps = self._build_requirements_list(detection_result)

        if (self.project_dir / "requirements.txt").exists():
            click.echo(f"   发现 requirements.txt，正在合并...")
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

    def _build_requirements_list(self, detection_result) -> List[str]:
        final_deps = merge_requirement_lists(
            self._get_base_requirements(detection_result),
            self._bundled_runtime_requirements(),
        )

        user_requirements = self.project_dir / "requirements.txt"
        if user_requirements.exists():
            user_content = user_requirements.read_text(encoding="utf-8")
            user_deps = exclude_requirement_names(
                parse_requirements_text(user_content),
                excluded_names={"ksadk"},
            )
            final_deps = merge_requirement_lists(final_deps, user_deps)

        return final_deps
    
    def _get_base_requirements(self, detection_result) -> List[str]:
        """获取基础依赖列表"""
        deps = [
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
        deps += requirements_for_framework(framework)
        
        return deps
    
    # 目标 Python 版本 (必须与容器运行时一致)
    TARGET_PYTHON_VERSION = "312"  # 容器中为 Python 3.12

    def _install_progress_stage(self, line: str) -> tuple[int, str] | None:
        normalized = line.strip()
        if not normalized:
            return None

        patterns = (
            ("Looking in indexes:", (8, "检查依赖源")),
            ("Collecting ", (18, "解析依赖")),
            ("Preparing metadata", (28, "准备元数据")),
            ("Using cached ", (40, "下载依赖")),
            ("Downloading ", (45, "下载依赖")),
            ("Building wheel for ", (76, "构建 wheel")),
            ("Requirement already satisfied:", (82, "复用已安装依赖")),
            ("Installing collected packages:", (86, "安装依赖")),
            ("Successfully installed ", (92, "安装完成")),
        )
        for prefix, value in patterns:
            if normalized.startswith(prefix):
                return value
        return None

    def _truncate_progress_summary(self, summary: str, max_length: int = 80) -> str:
        normalized = " ".join(summary.strip().split())
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3] + "..."

    def _format_elapsed(self, started_at: float) -> str:
        elapsed = max(0.0, time.monotonic() - started_at)
        if elapsed < 60:
            return f"{elapsed:.1f}s"
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        return f"{minutes}m{seconds:02d}s"

    def _render_install_progress(self, percent: int, stage: str, summary: str = "") -> str:
        clamped = max(0, min(percent, 100))
        filled = round((clamped / 100) * self.INSTALL_PROGRESS_BAR_WIDTH)
        if filled <= 0:
            bar = "." * self.INSTALL_PROGRESS_BAR_WIDTH
        elif filled >= self.INSTALL_PROGRESS_BAR_WIDTH:
            bar = "=" * self.INSTALL_PROGRESS_BAR_WIDTH
        else:
            bar = "=" * (filled - 1) + ">" + "." * (self.INSTALL_PROGRESS_BAR_WIDTH - filled)
        message = f"   [{bar}] {clamped:>3}% {stage}"
        if summary:
            message += f" | {self._truncate_progress_summary(summary)}"
        return message

    def _emit_install_progress(self, percent: int, stage: str, summary: str = "") -> None:
        clamped = max(0, min(percent, 100))
        if clamped < self._install_progress_percent:
            return

        line = self._render_install_progress(clamped, stage, summary)
        if line == self._install_progress_last_line:
            return

        self._install_progress_percent = clamped
        self._install_progress_stage_name = stage
        self._install_progress_summary_text = summary
        padding = " " * max(0, self._install_progress_width - len(line))
        if sys.stdout.isatty():
            click.echo(f"\r{line}{padding}", nl=False)
            self._install_progress_width = len(line)
        else:
            click.echo(line)
            self._install_progress_width = 0
        self._install_progress_last_line = line

    def _adjust_install_progress_percent(self, percent: int, stage: str) -> int:
        count = self._install_progress_event_counts.get(stage, 0)
        if count <= 0:
            return percent

        if stage == "下载依赖":
            base = 45 if percent >= 45 else percent
            return min(
                self.INSTALL_DOWNLOAD_PROGRESS_MAX,
                max(
                    percent,
                    base + min(self.INSTALL_DOWNLOAD_PROGRESS_MAX - base, int(count * 0.8)),
                ),
            )

        return percent

    def _finish_install_progress(self) -> None:
        if self._install_progress_width and sys.stdout.isatty():
            click.echo()
        self._install_progress_width = 0
        self._install_progress_percent = 0
        self._install_progress_stage_name = ""
        self._install_progress_summary_text = ""
        self._install_progress_last_line = ""
        self._install_progress_event_counts = {}
        self._install_progress_started_at = time.monotonic()

    def _result_error_summary(self, result: subprocess.CompletedProcess[str] | None) -> str:
        if result is None:
            return "unknown"
        combined = "\n".join(
            part for part in (result.stderr or "", result.stdout or "") if part
        )
        for line in reversed(combined.splitlines()):
            normalized = line.strip()
            if normalized:
                return self._truncate_progress_summary(normalized)
        return "unknown"

    def _pip_missing_from_result(self, result: subprocess.CompletedProcess[str] | None) -> bool:
        if result is None:
            return False
        combined = "\n".join(
            part for part in (result.stderr or "", result.stdout or "") if part
        )
        return "No module named pip" in combined

    def _bootstrap_pip_if_missing(self, result: subprocess.CompletedProcess[str] | None) -> bool:
        if not self._pip_missing_from_result(result):
            return False

        self._emit_install_progress(
            14,
            "准备安装",
            "pip 工具链缺失，正在自动初始化",
        )
        bootstrap_cmd = [
            sys.executable,
            "-m",
            "ensurepip",
            "--upgrade",
        ]
        try:
            bootstrap_result = subprocess.run(
                bootstrap_cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception as exc:
            self._emit_install_progress(
                14,
                "准备安装",
                f"pip 自动初始化失败: {exc}",
            )
            return False

        if bootstrap_result.returncode == 0:
            self._emit_install_progress(
                15,
                "准备安装",
                "pip 工具链已初始化，继续安装依赖",
            )
            return True

        self._emit_install_progress(
            14,
            "准备安装",
            f"pip 自动初始化失败: {self._result_error_summary(bootstrap_result)}",
        )
        return False

    def _extract_pip_artifact_name(self, value: str) -> str:
        token = value.strip().split()[0] if value.strip() else ""
        parsed = urlparse(token)
        if parsed.scheme and parsed.path:
            basename = os.path.basename(parsed.path)
            if basename:
                return basename
        basename = os.path.basename(token)
        return basename or token or value.strip()

    def _summarize_pip_progress_line(self, percent: int, stage: str, line: str) -> Optional[str]:
        normalized = line.strip()
        if not normalized:
            return None

        force_emit = percent > self._install_progress_percent or stage != self._install_progress_stage_name
        count = self._install_progress_event_counts.get(stage, 0) + 1
        self._install_progress_event_counts[stage] = count

        if stage == "检查依赖源":
            return self._truncate_progress_summary(normalized)

        if stage == "解析依赖":
            detail = normalized[len("Collecting "):].split(" (from ", 1)[0]
            summary = f"已解析 {count} 个依赖，最近: {detail}"
            if force_emit or count in self.INSTALL_PROGRESS_EVENT_MILESTONES or count % 10 == 0:
                return summary
            return None

        if stage == "准备元数据":
            if force_emit or count in {1, 3} or count % 10 == 0:
                return f"准备元数据: {self._truncate_progress_summary(normalized)}"
            return None

        if stage == "下载依赖":
            if normalized.startswith("Using cached "):
                payload = normalized[len("Using cached "):]
            elif normalized.startswith("Downloading "):
                payload = normalized[len("Downloading "):]
            else:
                payload = normalized
            target = self._extract_pip_artifact_name(payload)
            summary = f"已处理 {count} 个 wheel，耗时 {self._format_elapsed(self._install_progress_started_at)}，最近: {target}"
            if force_emit or count in self.INSTALL_PROGRESS_EVENT_MILESTONES or count % 5 == 0:
                return summary
            return None

        if stage == "构建 wheel":
            detail = normalized[len("Building wheel for "):].split(" ", 1)[0]
            if force_emit or count == 1 or count % 5 == 0:
                return f"构建 wheel: {detail}"
            return None

        if stage == "安装依赖":
            detail = normalized[len("Installing collected packages:"):].strip()
            return f"安装包: {self._truncate_progress_summary(detail, max_length=60)}"

        if stage == "复用已安装依赖":
            detail = normalized[len("Requirement already satisfied:"):].split(" in ", 1)[0].strip()
            if force_emit or count in self.INSTALL_PROGRESS_EVENT_MILESTONES or count % 10 == 0:
                return f"复用依赖: {detail}"
            return None

        if stage == "安装完成":
            detail = normalized[len("Successfully installed "):].strip()
            return f"最近结果: {self._truncate_progress_summary(detail, max_length=60)}"

        return self._truncate_progress_summary(normalized)

    def _pip_index_cache_path(self) -> Path:
        return Path.home() / ".agentengine" / "pip-index-cache.json"

    def _normalize_pip_index_url(self, index_url: str) -> str:
        return index_url.rstrip("/")

    def _short_pip_index_label(self, index_url: str) -> str:
        parsed = urlparse(index_url)
        if not parsed.scheme:
            return index_url
        label = parsed.netloc
        path = parsed.path.rstrip("/")
        if path:
            label += path
        return label

    def _load_cached_pip_index_order(self, urls: list[str]) -> Optional[list[str]]:
        cache_path = self._pip_index_cache_path()
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return None

        if payload.get("version") != self.PIP_INDEX_CACHE_VERSION:
            return None

        updated_at = payload.get("updated_at")
        if not isinstance(updated_at, (int, float)):
            return None
        if time.time() - float(updated_at) > self.PIP_INDEX_CACHE_TTL_SECONDS:
            return None

        cached_order = [
            self._normalize_pip_index_url(item)
            for item in payload.get("order", [])
            if isinstance(item, str)
        ]
        required = {self._normalize_pip_index_url(url) for url in urls}
        if not required.issubset(cached_order):
            return None

        return [item for item in cached_order if item in required]

    def _save_pip_index_order(self, order: list[str]) -> None:
        cache_path = self._pip_index_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": self.PIP_INDEX_CACHE_VERSION,
                        "updated_at": time.time(),
                        "order": order,
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except Exception:
            return

    def _probe_pip_index_latency(self, index_url: str) -> Optional[float]:
        probe_url = f"{self._normalize_pip_index_url(index_url)}/pip/"
        request = Request(
            probe_url,
            headers={"User-Agent": "ksadk-build/0.4.0"},
        )
        started_at = time.monotonic()
        try:
            with urlopen(request, timeout=self.PIP_INDEX_PROBE_TIMEOUT_SECONDS) as response:
                response.read(1)
            return time.monotonic() - started_at
        except Exception:
            return None

    def _rank_pip_index_urls(self, urls: list[str]) -> tuple[list[str], str]:
        cached_order = self._load_cached_pip_index_order(urls)
        if cached_order:
            best = cached_order[0]
            return cached_order, f"缓存优先 {self._short_pip_index_label(best)}"

        timings: dict[str, Optional[float]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(urls))) as executor:
            future_to_url = {
                executor.submit(self._probe_pip_index_latency, url): url
                for url in urls
            }
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    timings[url] = future.result()
                except Exception:
                    timings[url] = None

        successful = [url for url in urls if timings.get(url) is not None]
        if not successful:
            return urls, "测速失败，按默认顺序尝试源"

        successful.sort(key=lambda item: timings[item] or float("inf"))
        failed = [url for url in urls if timings.get(url) is None]
        ordered = [*successful, *failed]
        self._save_pip_index_order(ordered)

        best = ordered[0]
        best_ms = round((timings[best] or 0.0) * 1000)
        return ordered, f"测速优先 {self._short_pip_index_label(best)} ({best_ms} ms)"
    
    def _install_dependencies(self, requirements_path: Path) -> bool:
        """安装依赖到 deps_dir"""
        current_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        target_python = self._target_python_display()
        if current_python != target_python:
            click.echo(
                f"   ⚠ 构建机 Python={current_python}，目标运行时 Python={target_python}，"
                "将执行二进制替换与 ABI 校验"
            )

        try:
            need_binary_replacement = (
                sys.platform in ("darwin", "win32")
                or (sys.platform.startswith("linux") and current_python != target_python)
            )
            used_target_runtime_wheels = False
            result = None

            if need_binary_replacement:
                self._emit_install_progress(
                    4,
                    "准备安装",
                    f"目标运行时 Python {target_python}",
                )
                result = self._run_pip_install(
                    requirements_path,
                    target_runtime_wheels=True,
                )
                if result.returncode == 0:
                    used_target_runtime_wheels = True
                else:
                    self._emit_install_progress(
                        34,
                        "回退安装",
                        "目标运行时 wheel 安装失败，改用宿主机构建并替换二进制",
                    )
                    if self.deps_dir.exists():
                        shutil.rmtree(self.deps_dir)
                    self.deps_dir.mkdir(parents=True, exist_ok=True)

            if result is None or result.returncode != 0:
                self._emit_install_progress(
                    10,
                    "准备安装",
                    "开始宿主机依赖安装",
                )
                result = self._run_pip_install(
                    requirements_path,
                    target_runtime_wheels=False,
                )
            
            if result.returncode != 0:
                self._finish_install_progress()
                click.secho("   ✗ 安装失败", fg='red')
                if result.stderr:
                    error_lines = [l for l in result.stderr.split('\n') if 'ERROR' in l.upper()][:3]
                    for line in error_lines:
                        click.echo(f"   {line}")
                return False
            
            # 替换非 Linux 平台二进制，或 Linux 下非目标 Python ABI 的二进制
            if need_binary_replacement and not used_target_runtime_wheels:
                self._emit_install_progress(
                    88,
                    "替换 Linux wheels",
                    "检测并替换平台相关二进制",
                )
                self._replace_platform_binaries()
            
            # 二进制兼容性校验（避免把不可运行的包部署到 Linux Runtime）
            self._emit_install_progress(
                96,
                "校验运行时兼容性",
                "扫描关键原生扩展",
            )
            incompatibles = self._scan_incompatible_binaries_in_deps()
            if incompatibles:
                self._finish_install_progress()
                click.secho("   ✗ 检测到与 Linux Runtime 不兼容的关键二进制，构建终止", fg='red')
                for item in incompatibles[:10]:
                    click.echo(f"      - {item}")
                if any(i.startswith("python-abi-mismatch:") for i in incompatibles):
                    click.echo(
                        f"   提示: 目标运行时为 Python {target_python}，"
                        f"请使用 Python {target_python} 构建，或改用 Container 模式部署"
                    )
                if any("tiktoken" in i for i in incompatibles):
                    click.echo("   提示: tiktoken 为 langchain-openai 必需；若替换失败可重试或检查网络/镜像")
                click.echo("   建议: 删除 .agentengine/*_build 后重新构建，或在 Linux 环境重新打包")
                return False
            
            deps_count = sum(1 for _ in self.deps_dir.rglob('*') if _.is_file())
            deps_size = sum(f.stat().st_size for f in self.deps_dir.rglob('*') if f.is_file()) / (1024 * 1024)
            self._emit_install_progress(
                100,
                "依赖安装完成",
                f"{deps_count} 个文件, {deps_size:.1f} MB",
            )
            self._finish_install_progress()
            
            return True
            
        except subprocess.TimeoutExpired:
            self._finish_install_progress()
            timeout_minutes = max(1, round(self._pip_install_timeout_seconds() / 60))
            click.secho(f"   ✗ 安装超时 ({timeout_minutes}分钟)", fg='red')
            return False
        except Exception as e:
            self._finish_install_progress()
            click.secho(f"   ✗ 依赖安装失败: {e}", fg='red')
            return False
    
    def _replace_platform_binaries(self) -> None:
        """替换非目标运行时平台/ABI 的 C 扩展为 Linux 目标版本。"""
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
        elif sys.platform.startswith('linux'):
            binary_files = list(self.deps_dir.rglob('*.so'))
        
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
        total_packages = len(packages_to_replace)
        for index, pkg_name in enumerate(sorted(packages_to_replace), start=1):
            # 提取确切的版本号以避免不兼容问题 (如 pydantic-core)
            target_version = ""
            search_name = pkg_name.replace('-', '_').lower()
            for info_dir in self.deps_dir.glob(f"{search_name}-*.dist-info"):
                version_str = info_dir.name[len(search_name)+1:-10]  # remove search_name + '-' and '.dist-info'
                target_version = f"=={version_str}"
                break
                
            pkg_with_version = f"{pkg_name}{target_version}"
            self._emit_install_progress(
                88 + min(6, round((index / max(total_packages, 1)) * 6)),
                "替换 Linux wheels",
                f"{index}/{total_packages} {pkg_with_version}",
            )
            
            try:
                downloaded = False
                for index_url in self._pip_index_candidates():
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
                        "--cache-dir", str(self.pip_cache_dir),
                    ]
                    if index_url:
                        download_cmd += ["-i", index_url]
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

    def _target_python_display(self) -> str:
        if len(self.TARGET_PYTHON_VERSION) >= 2:
            major = self.TARGET_PYTHON_VERSION[0]
            minor = self.TARGET_PYTHON_VERSION[1:]
            return f"{major}.{minor}"
        return self.TARGET_PYTHON_VERSION

    def _is_target_python_abi_binary(self, name: str) -> bool:
        lower = name.lower()
        if not lower.endswith(".so"):
            return False
        if "abi3" in lower:
            return True
        if "cpython-" in lower:
            return f"cpython-{self.TARGET_PYTHON_VERSION}" in lower
        # 无 ABI tag 的 .so 无法准确识别，按可用处理，避免误伤
        return True

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
                linux_bins = [n for n in matched_bins if self._is_linux_so(n)]
                if not linux_bins:
                    issues.append(f"missing-linux:{module_name}")
                    continue
                if not any(self._is_target_python_abi_binary(n) for n in linux_bins):
                    issues.append(
                        f"python-abi-mismatch:{module_name}:"
                        f"expected-cpython-{self.TARGET_PYTHON_VERSION}-or-abi3"
                    )
        
        # 通用检查: 所有 .so 文件中不应包含 darwin/win 平台标识
        all_so_files = [n for n in names if n.endswith('.so')]
        darwin_so_count = sum(1 for n in all_so_files if 'darwin' in n.lower())
        if darwin_so_count > 0:
            issues.append(f"warning:found-{darwin_so_count}-darwin-so-files")
        
        return issues
    
    def _package_zip(self, zip_path: Path, detection_result) -> None:
        """打包 zip 文件"""
        project_files = list(self._iter_project_files())
        dependency_files = [file_path for file_path in self.deps_dir.rglob("*") if file_path.is_file()]
        bundled_source_files = list(self._iter_bundled_source_files())
        dependency_size = sum(file_path.stat().st_size for file_path in dependency_files)
        if dependency_files:
            click.echo(
                f"   打包输入: {len(project_files)} 个项目文件 + "
                f"{len(dependency_files)} 个依赖文件 ({dependency_size / (1024 * 1024):.1f} MB) + "
                f"{len(bundled_source_files)} 个 runtime 文件"
            )

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            # 添加项目文件
            for file_path in project_files:
                arcname = file_path.relative_to(self.project_dir).as_posix()
                zf.write(file_path, arcname)
            
            # 添加依赖
            deps_count = 0
            for file_path in dependency_files:
                arcname = file_path.relative_to(self.deps_dir).as_posix()
                zf.write(file_path, arcname)
                deps_count += 1
                self._emit_package_progress("打包依赖", deps_count, len(dependency_files))
            self._finish_package_progress()
            
            # 添加随运行时下发的 ksadk 源码
            bundled_source_count = 0
            for package_name, relative, file_path in bundled_source_files:
                arcname = f"{package_name}/{relative}"
                zf.write(file_path, arcname)
                bundled_source_count += 1
                self._emit_package_progress(
                    "打包 runtime",
                    bundled_source_count,
                    len(bundled_source_files),
                )
            self._finish_package_progress()
            
            click.echo(f"   ✓ 打包运行时源码: {bundled_source_count} 个文件")
            
            # 添加 entrypoint
            entrypoint_content = self._generate_entrypoint(detection_result)
            zf.writestr("entrypoint.py", entrypoint_content)
        
        click.echo(f"   ✓ 打包完成: {len(project_files)} 个项目文件 + {deps_count} 个依赖文件")
        self._emit_package_size_report(zip_path)

    def _emit_package_size_report(self, zip_path: Path, *, limit: int = 8) -> None:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                infos = zf.infolist()
        except Exception:
            return

        if not infos:
            return

        by_top_level: dict[str, int] = {}
        for item in infos:
            name = item.filename.strip("/")
            if not name:
                continue
            top_level = name.split("/", 1)[0]
            by_top_level[top_level] = by_top_level.get(top_level, 0) + item.file_size

        self._emit_package_size_report_from_entries(
            raw_total=sum(item.file_size for item in infos),
            compressed_total=sum(item.compress_size for item in infos),
            by_top_level=by_top_level,
            limit=limit,
        )

    def _emit_package_size_report_from_entries(
        self,
        *,
        raw_total: int,
        compressed_total: int,
        by_top_level: dict[str, int],
        limit: int = 8,
    ) -> None:
        click.echo(
            "   包体积: "
            f"zip {compressed_total / (1024 * 1024):.1f} MB / "
            f"解压 {raw_total / (1024 * 1024):.1f} MB"
        )
        if by_top_level:
            top_items = sorted(by_top_level.items(), key=lambda item: item[1], reverse=True)[:limit]
            summary = ", ".join(
                f"{name} {size / (1024 * 1024):.1f} MB"
                for name, size in top_items
            )
            click.echo(f"   体积 Top{len(top_items)}: {summary}")
        if (
            raw_total > self.CONTAINER_SUGGESTION_RAW_THRESHOLD_BYTES
            or compressed_total > self.CONTAINER_SUGGESTION_ZIP_THRESHOLD_BYTES
        ):
            click.secho(
                "   ⚠ 包体积较大，建议使用 container 模式构建并复用镜像层: "
                "agentengine build . --mode container --push --registry <registry>",
                fg="yellow",
            )

    def _emit_package_progress(self, label: str, current: int, total: int) -> None:
        if total < 500:
            return

        percent = max(0, min(100, int((current / total) * 100)))
        if not self._should_emit_package_progress(label, percent, current, total):
            return

        line = self._render_package_progress(label, percent, current, total)
        if line == self._package_progress_last_line:
            return

        padding = " " * max(0, self._package_progress_width - len(line))
        if sys.stdout.isatty():
            click.echo(f"\r{line}{padding}", nl=False)
            self._package_progress_width = len(line)
        else:
            click.echo(line)
            self._package_progress_width = 0
        self._package_progress_last_line = line

    def _should_emit_package_progress(
        self,
        label: str,
        percent: int,
        current: int,
        total: int,
    ) -> bool:
        if sys.stdout.isatty():
            previous = self._package_progress_last_percent_by_label.get(label, -1)
            if current == total or percent > previous:
                self._package_progress_last_percent_by_label[label] = percent
                return True
            return False

        previous_milestone = self._package_progress_logged_milestone_by_label.get(label, 0)
        for milestone in self.PACKAGE_PROGRESS_LOG_MILESTONES:
            if previous_milestone < milestone <= percent:
                self._package_progress_logged_milestone_by_label[label] = milestone
                return True
        return False

    def _render_package_progress(self, label: str, percent: int, current: int, total: int) -> str:
        filled = round((percent / 100) * self.PACKAGE_PROGRESS_BAR_WIDTH)
        if filled <= 0:
            bar = "." * self.PACKAGE_PROGRESS_BAR_WIDTH
        elif filled >= self.PACKAGE_PROGRESS_BAR_WIDTH:
            bar = "=" * self.PACKAGE_PROGRESS_BAR_WIDTH
        else:
            bar = "=" * (filled - 1) + ">" + "." * (self.PACKAGE_PROGRESS_BAR_WIDTH - filled)
        return f"   [{bar}] {percent:>3}% {label} | {current}/{total} files"

    def _finish_package_progress(self) -> None:
        if self._package_progress_width and sys.stdout.isatty():
            click.echo()
        self._package_progress_width = 0
        self._package_progress_last_line = ""
        self._package_progress_last_percent_by_label = {}
        self._package_progress_logged_milestone_by_label = {}
    
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
CODE_SRC = os.path.join(CODE_ROOT, "src")
if os.path.isdir(CODE_SRC):
    sys.path.insert(0, CODE_SRC)
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
    package_path=os.path.join(CODE_ROOT, "{package_name}"),
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
runner = create_runner(detection_result, CODE_ROOT)
runner.load_agent()
set_runner(runner)
logger.info("Agent 加载成功!")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"启动 HTTP Server: 0.0.0.0:{{port}}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level=LOG_LEVEL.lower())
'''

    def _pip_index_candidates(self) -> List[Optional[str]]:
        """返回 pip index 尝试顺序。显式环境变量优先，其次是官方与内置镜像源。"""
        if self._pip_index_candidates_cache is not None:
            return list(self._pip_index_candidates_cache)

        env_index = os.environ.get("PIP_INDEX_URL") or os.environ.get("UV_INDEX_URL")
        if env_index:
            candidates: list[Optional[str]] = [None]
            seen = {self._normalize_pip_index_url(env_index)}
            for candidate in self.PIP_INDEX_FALLBACKS:
                normalized = self._normalize_pip_index_url(candidate)
                if normalized in seen:
                    continue
                seen.add(normalized)
                candidates.append(normalized)
            self._pip_index_selection_summary = (
                f"使用显式依赖源 {self._short_pip_index_label(env_index)}"
            )
            self._pip_index_candidates_cache = candidates
            return list(candidates)

        seen: Set[str] = set()
        urls: list[str] = []
        for candidate in self.PIP_INDEX_FALLBACKS:
            normalized = self._normalize_pip_index_url(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            urls.append(normalized)

        ordered, summary = self._rank_pip_index_urls(urls)
        self._pip_index_selection_summary = summary
        self._pip_index_candidates_cache = list(ordered)
        return list(self._pip_index_candidates_cache)

    def _pip_index_label(self, index_url: Optional[str]) -> str:
        if index_url:
            return self._short_pip_index_label(index_url)
        env_index = os.environ.get("PIP_INDEX_URL") or os.environ.get("UV_INDEX_URL")
        if env_index:
            return f"default pip index ({self._short_pip_index_label(env_index)})"
        return "default pip index"

    def _run_pip_install(self, requirements_path: Path, *, target_runtime_wheels: bool):
        index_candidates = self._pip_index_candidates()
        if self._pip_index_selection_summary:
            self._emit_install_progress(
                6 if target_runtime_wheels else 11,
                "选择依赖源",
                self._pip_index_selection_summary,
            )
        result = None
        for index_url in index_candidates:
            source_label = self._pip_index_label(index_url)
            install_mode = "目标运行时 wheel" if target_runtime_wheels else "宿主机依赖"
            self._emit_install_progress(
                8 if target_runtime_wheels else 12,
                "准备安装",
                f"{install_mode}，源 {source_label}",
            )
            install_cmd = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements_path),
                "-t",
                str(self.deps_dir),
                "--disable-pip-version-check",
                "--no-warn-script-location",
                "--retries",
                "3",
                "--timeout",
                "60",
                "--cache-dir",
                str(self.pip_cache_dir),
            ]
            if target_runtime_wheels:
                for platform in self.TARGET_INSTALL_PLATFORMS:
                    install_cmd += ["--platform", platform]
                install_cmd += [
                    "--python-version",
                    self.TARGET_PYTHON_VERSION,
                    "--implementation",
                    "cp",
                    "--only-binary=:all:",
                ]
            if index_url:
                install_cmd += ["-i", index_url]
            result = self._run_streamed_pip_install(
                install_cmd,
                timeout=self._pip_install_timeout_seconds(),
            )
            if result.returncode != 0 and self._bootstrap_pip_if_missing(result):
                result = self._run_streamed_pip_install(
                    install_cmd,
                    timeout=self._pip_install_timeout_seconds(),
                )
            if result.returncode == 0:
                break
            self._emit_install_progress(
                22 if target_runtime_wheels else 18,
                "切换依赖源",
                f"{source_label} 失败：{self._result_error_summary(result)}",
            )
        return result

    def _run_streamed_pip_install(
        self,
        install_cmd: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        output_lines: list[str] = []
        self._install_progress_event_counts = {}
        self._install_progress_started_at = time.monotonic()
        process = subprocess.Popen(
            install_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _consume_output() -> None:
            if process.stdout is None:
                return
            try:
                for raw_line in process.stdout:
                    output_lines.append(raw_line)
                    progress = self._install_progress_stage(raw_line)
                    if progress:
                        percent, stage = progress
                        summary = self._summarize_pip_progress_line(percent, stage, raw_line)
                        if summary:
                            adjusted_percent = self._adjust_install_progress_percent(percent, stage)
                            self._emit_install_progress(adjusted_percent, stage, summary)
            except ValueError:
                return

        reader = threading.Thread(target=_consume_output, daemon=True)
        reader.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            reader.join(timeout=1)
            raise

        reader.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
        stdout = "".join(output_lines)
        stderr = stdout if returncode != 0 else ""
        return subprocess.CompletedProcess(
            install_cmd,
            returncode,
            stdout,
            stderr,
        )
