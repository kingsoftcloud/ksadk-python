from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ksadk.skills.loader import LocalSkill
from ksadk.skills.runtime.artifacts import (
    collect_output_dir_artifacts,
    merge_artifacts,
    parse_artifact_lines,
)
from ksadk.skills.runtime.base import normalize_skill_names


@dataclass
class WorkflowExecution:
    status: str
    executed_skill: str = ""
    output_files: list[str] = field(default_factory=list)
    commands: list[dict[str, object]] = field(default_factory=list)
    selected_skills: list[str] = field(default_factory=list)
    loaded_skills: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    artifact_bundle: dict[str, object] | None = None
