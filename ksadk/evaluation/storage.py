"""Local, atomic storage for the shared evaluation report contract."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import EvalRunReport


class EvaluationStorageError(RuntimeError):
    """Raised when a report cannot be read or safely persisted."""


class EvaluationStorage:
    """Persist reports below one workspace-owned evaluation directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def report_path(self, run_id: str) -> Path:
        """Return the only report location allowed for a run identifier."""

        if not run_id or Path(run_id).name != run_id:
            raise EvaluationStorageError("run_id 只能包含文件名，不允许路径穿越")
        return self.root / run_id / "report.json"

    def write_report(self, report: EvalRunReport) -> Path:
        """Write the completed report atomically after its artifact directory exists."""

        path = self.report_path(report.spec.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{report.report_digest}.tmp")
        payload = json.dumps(
            report.model_dump(mode="json", by_alias=True, exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise EvaluationStorageError("评测报告写入失败") from exc
        return path

    def read_report(self, run_id: str) -> EvalRunReport:
        path = self.report_path(run_id)
        try:
            return EvalRunReport.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EvaluationStorageError("评测报告不存在") from exc
        except (OSError, ValueError) as exc:
            raise EvaluationStorageError("评测报告损坏或不可读") from exc

    def list_reports(self) -> list[EvalRunReport]:
        if not self.root.is_dir():
            return []
        reports: list[EvalRunReport] = []
        for path in sorted(self.root.glob("*/report.json")):
            try:
                reports.append(
                    EvalRunReport.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                continue
        return reports
