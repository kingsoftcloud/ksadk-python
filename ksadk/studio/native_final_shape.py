"""A rejection-only Studio verifier for explicit final-answer shape requests.

The host can reject an obvious shape violation and send safe repair feedback.
Passing this check never proves the user's semantic goal, so the required
acceptance remains ``not_run`` and the Native outcome remains ``unverified``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from ksadk.harness.native.completion import (
    CompletionCandidate,
    VerificationItemResult,
    VerificationReport,
)
from ksadk.harness.native.ports import VerificationBundle, VerificationRequest
from ksadk.harness.native.state import RunScope
from ksadk.harness.stores.native_contract import RunStore
from ksadk.studio.native_browser_port import StudioNativeBrowserPort, browser_environment_warning
from ksadk.studio.native_output_fields import declared_object_fields

VERIFIER_REF = "studio.final-answer-shape-rejection"
VERIFIER_VERSION = "12"
BROWSER_VERIFIER_REF = "studio.browser-smoke-rejection"
BROWSER_VERIFIER_VERSION = "9"
ARTIFACT_VERIFIER_REF = "studio.exact-workspace-file-observation"
ARTIFACT_VERIFIER_VERSION = "1"
# New goals bind rejection-only checks independently of installed capabilities.
# Legacy shape/browser contracts retain their pinned semantics on restoration.
EVIDENCE_VERIFIER_REF = "studio.delivery-evidence-rejection"
EVIDENCE_VERIFIER_VERSION = "1"
FinalShape = (
    Literal[
        "number", "integer", "digits", "sentence", "json_boolean", "json_object",
        "json_array", "python_list"
    ]
    | tuple[str, str]
)


def json_object_submission_schema(goal: str | None = None) -> dict:
    """One host submission protocol for explicit parent and child object goals.

    Freeze the outer shape and an unambiguous explicit key list, never an
    inferred answer, field value/type or semantic verifier. Every caller
    receives a fresh schema. Unsupported field declarations remain unconstrained.
    """
    output = {
        "type": "object",
        "description": "The complete JSON object requested by the current user.",
    }
    fields = declared_object_fields(_structured_json_clauses(goal)) if goal else None
    if fields:
        output.update(
            properties={key: {} for key in fields},
            required=list(fields),
            additionalProperties=False,
        )
    return {
        "type": "object",
        "properties": {"output": output},
        "required": ["output"],
        "additionalProperties": False,
    }


def declared_value_submission_schema(goal: str) -> dict | None:
    """Compile an explicit delivery form, never its answer or source semantics."""
    shape = requested_final_shape(goal)
    if shape == "json_object":
        return json_object_submission_schema(goal)
    types = {"number": "number", "integer": "integer", "json_boolean": "boolean",
             "json_array": "array", "sentence": "string", "digits": "string"}
    if not isinstance(shape, str) or shape not in types:
        return None
    value = {"type": types[shape]}
    if shape == "sentence":
        value.update(minLength=1, pattern=r"^[^\r\n]+$", description=(
            "Only the complete requested sentence, without an introduction or commentary."
        ))
    elif shape == "digits":
        value.update(minLength=1, pattern=r"^[0-9]+$")
    return {"type": "object", "properties": {"output": value},
            "required": ["output"], "additionalProperties": False}


@dataclass(frozen=True)
class ExactWorkspaceFile:
    relative_path: str
    content: str


_EXACT_WORKSPACE_FILE = re.compile(
    r"\s*(?:请)?在工作区(?:创建|写入)\s*"
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+)\s*[,，]\s*"
    r"内容(?:必须)?(?:恰好是|完全是|只写|仅写)\s*"
    r"(?P<content>[^。\r\n]{1,4096})。"
    r"(?:完成后(?:再)?回复(?:文件)?路径。?)?\s*"
)


def requested_exact_workspace_file(goal: str) -> ExactWorkspaceFile | None:
    """Only freeze an explicit path and byte-for-byte content, never infer them."""
    match = _EXACT_WORKSPACE_FILE.fullmatch(goal)
    if match is None:
        return None
    relative = Path(match.group("path"))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return ExactWorkspaceFile(relative.as_posix(), match.group("content"))


def encode_exact_workspace_file(requirement: ExactWorkspaceFile) -> str:
    return json.dumps(
        {"relative_path": requirement.relative_path, "content": requirement.content},
        ensure_ascii=False,
        sort_keys=True,
    )


def exact_byte_difference(expected: bytes, actual: bytes) -> dict[str, int | str | None]:
    """Give bounded byte-level repair evidence without echoing file contents."""
    offset = next(
        (index for index, (left, right) in enumerate(zip(expected, actual)) if left != right),
        min(len(expected), len(actual)),
    )
    return {
        "first_mismatch_byte": offset,
        "expected_byte_hex": f"{expected[offset]:02x}" if offset < len(expected) else None,
        "actual_byte_hex": f"{actual[offset]:02x}" if offset < len(actual) else None,
        "expected_byte_length": len(expected),
        "actual_byte_length": len(actual),
    }


def _read_exact_workspace_file(root: Path, requirement: ExactWorkspaceFile) -> tuple[bool, str]:
    resolved_root = root.resolve()
    target = resolved_root / requirement.relative_path
    try:
        inside = target.resolve(strict=False).is_relative_to(resolved_root)
    except (OSError, RuntimeError):
        inside = False
    if not inside:
        return False, "Requested file is outside the bound workspace."
    try:
        actual = target.read_bytes()
    except (OSError, ValueError):
        return False, "Requested file is missing or unreadable."
    expected = requirement.content.encode("utf-8")
    if actual != expected:
        difference = exact_byte_difference(expected, actual)
        return False, (
            "Requested file bytes differ from the user's literal content at byte "
            f"{difference['first_mismatch_byte']}: expected "
            f"{difference['expected_byte_hex']}, actual {difference['actual_byte_hex']} "
            "(UTF-8 hex)."
        )
    return True, hashlib.sha256(actual).hexdigest()


def _decode_exact_workspace_file(value: str) -> ExactWorkspaceFile | None:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    relative = parsed.get("relative_path")
    content = parsed.get("content")
    if (
        not isinstance(relative, str)
        or not isinstance(content, str)
        or not re.fullmatch(r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", relative)
        or any(part in {"", ".", ".."} for part in Path(relative).parts)
        or not 1 <= len(content) <= 4096
    ):
        return None
    return ExactWorkspaceFile(relative, content)


_NUMBER_REQUEST = re.compile(
    r"\b(?:return|reply|respond|output|give)\s+only\s+"
    r"(?:the|a|one)?\s*(?P<kind>total|sum|count|number|integer|numeric\s+value)"
    r"(?:\s+(?:it|they|the\s+[a-z-]+)\s+(?:reported|returned|produced))?"
    r"\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_DIGITS_REQUEST = re.compile(
    r"\b(?:return|reply|respond|output|give)(?:\s+with)?\s+only\s+"
    r"(?:the|those|these)?\s*digits?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_SENTENCE_REQUEST = re.compile(
    r"\b(?:return|reply|respond|output|give)\s+only\s+"
    r"(?:the|a|one)\s+(?:[a-z-]+\s+){0,4}sentence\b"
    r"(?:\s+from\s+[a-z0-9_./-]+)?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_NUMBER_ANSWER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)")
_INTEGER_ANSWER = re.compile(r"[+-]?\d+")
_CHINESE_NUMBER_REQUEST = re.compile(r"(?:只|仅)(?:回复|输出)\s*(?:数字|数值)\s*[。.!！]?\s*$")
_CHINESE_JSON_BOOLEAN_REQUEST = re.compile(
    r"(?:只|仅)(?:回复|输出)\s*JSON\s*布尔值\s*[。.!！]?\s*$", re.IGNORECASE
)
_CHINESE_CHOICE_REQUEST = re.compile(
    r"(?:只|仅)(?:回复|输出)\s*"
    r"(?P<first>[\w\u4e00-\u9fff]{1,16})\s*或\s*"
    r"(?P<second>[\w\u4e00-\u9fff]{1,16})\s*[。.!！]?\s*$"
)
_STRUCTURED_JSON_REQUEST = re.compile(
    r"\b(?:return|reply|respond|output|give)(?:\s+with)?\s+only\s+"
    r"(?:a|one|the)?\s*JSON\s+(?P<kind>object|array)\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_CHINESE_STRUCTURED_JSON_REQUEST = re.compile(
    r"\s*(?:(?:最后|最终)(?:请)?|请)?(?:只|仅)(?:回复|输出|返回)\s*"
    r"(?:一个|上述|该)?\s*JSON\s*"
    r"(?P<kind>对象|数组)(?:[,，]\s*(?:(?:键|字段|每项)[^。\n]*|"
    r"格式(?:为|是)\s*(?P<format>\{[^。\n]*\}|\[[^。\n]*\])|"
    r"(?P<header>(?:格式|结构)如下\s*[:：])|"
    r"(?:不(?:要|得)?|禁止)(?:附加|添加)(?:其他|额外|任何)?"
    r"(?:说明|解释)(?:文字|文本)?))?\s*",
    re.IGNORECASE,
)
_PYTHON_LIST_REQUEST = re.compile(
    r"(?:^|[.!?\n]\s*)(?:return|reply|respond|output|give)(?:\s+with)?\s+only\s+"
    r"(?:a|one|the)?\s*Python\s+list\s+literal\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_CHINESE_PYTHON_LIST_REQUEST = re.compile(
    r"\s*(?:最后)?(?:只|仅)(?:回复|输出|返回)\s*Python\s*列表字面量\s*",
    re.IGNORECASE,
)


def _python_list_requested(goal: str) -> bool:
    if _PYTHON_LIST_REQUEST.search(goal):
        return True
    # Only a complete directive counts. A quote, negation, or conflicting
    # request must not silently become a host constraint.
    return any(
        _CHINESE_PYTHON_LIST_REQUEST.fullmatch(sentence) is not None
        for sentence in re.split(r"[。！？\n]", goal)
    )


def _visible_goal_text(goal: str) -> str:
    """Quoted blocks and fenced examples cannot supply output authority."""
    visible, fence = [], None
    for line in goal.splitlines():
        marker = re.match(r"\s*(`{3,}|~{3,})(.*)$", line)
        if marker:
            kind, length = marker[1][0], len(marker[1])
            if fence is None:
                fence = kind, length
            elif fence[0] == kind and length >= fence[1] and not marker[2].strip():
                fence = None
            continue
        if fence is None and not line.lstrip().startswith(">"):
            visible.append(line)
    return "\n".join(visible)


def _directive_match(pattern: re.Pattern, text: str) -> re.Match | None:
    """A matching phrase needs an imperative clause, not just matching words."""
    match = pattern.search(text)
    if match is None:
        return None
    prefix = re.split(r"[.!?。？！\n,，]", text[:match.start()])[-1].strip().lower()
    direct = prefix in {
        "", "then", "finally", "and then", "请", "最后", "最终", "最后请", "最终请", "然后",
    }
    compound = re.fullmatch(
        r"(?:read|inspect|check|calculate|compute|use|ask|analy[sz]e)\b"
        r"[^\"'`:：“”‘’]*\s+(?:and(?:\s+then)?|then)", prefix,
    )
    return match if direct or compound else None


def _structured_json_clauses(goal: str) -> list[tuple[FinalShape, str]]:
    # Parse explicit directives only, never contradictory outer shapes.
    text = _visible_goal_text(goal)
    candidates = []
    if match := _directive_match(_STRUCTURED_JSON_REQUEST, text):
        candidates.append(
            ("json_object" if match.group("kind") == "object" else "json_array", match[0])
        )
    sentences = re.split(r"[。\n]", text)
    for index, sentence in enumerate(sentences):
        sentence = re.sub(r"^\s*(?:-\s+|[1-9][0-9]{0,2}[.)]\s+)", "", sentence)
        if match := _CHINESE_STRUCTURED_JSON_REQUEST.fullmatch(sentence):
            expected = "{" if match.group("kind") == "对象" else "["
            example = match.group("format")
            if match.group("header"):
                example = next((s.lstrip() for s in sentences[index + 1 :] if s.strip()), "")
                if not example.startswith(("{", "[")):
                    continue
            # Only validate the explicitly declared outer shape. Example
            # fields, values and task semantics are never inferred here.
            if example and example[0] != expected:
                return []
            candidates.append(("json_object" if expected == "{" else "json_array", match[0]))
    return candidates


def _structured_json_clause(goal: str) -> tuple[FinalShape, str] | None:
    candidates = _structured_json_clauses(goal)
    if not candidates or len({shape for shape, _ in candidates}) != 1:
        return None
    return candidates[-1]


def final_shape_contract_description(goal: str) -> str:
    """Keep an explicit output clause when bounding the acceptance description.

    The full goal remains in the Run's authoritative continuity state. This
    description is only a rejection rule, not proof of semantic completion.
    """
    suffix = goal[-1024:]
    if requested_final_shape(suffix) == requested_final_shape(goal):
        return suffix
    if structured := _structured_json_clause(goal):
        return structured[1][:1024]
    return suffix


def requested_final_shape(goal: str) -> FinalShape | None:
    """Recognize only an unambiguous final clause; unknown requests get no rule."""
    visible = _visible_goal_text(goal)
    if number := _directive_match(_NUMBER_REQUEST, visible):
        return "integer" if number["kind"].lower() == "integer" else "number"
    if _directive_match(_CHINESE_NUMBER_REQUEST, visible):
        return "number"
    if _directive_match(_CHINESE_JSON_BOOLEAN_REQUEST, visible):
        return "json_boolean"
    if choices := _directive_match(_CHINESE_CHOICE_REQUEST, visible):
        first, second = choices.group("first", "second")
        if first != second:
            return first, second
    if _directive_match(_DIGITS_REQUEST, visible):
        return "digits"
    if _directive_match(_SENTENCE_REQUEST, visible):
        return "sentence"
    if structured := _structured_json_clause(goal):
        return structured[0]
    if _python_list_requested(visible):
        return "python_list"
    return None


def _violation(shape: FinalShape, text: str) -> str | None:
    candidate = text.strip()
    if isinstance(shape, tuple):
        return (
            None
            if candidate in shape
            else (
                "Return exactly one of the two requested values, "
                "without punctuation or explanation."
            )
        )
    if shape == "number":
        return (
            None
            if _NUMBER_ANSWER.fullmatch(candidate)
            else "Return only one numeric value, without a preamble or Markdown."
        )
    if shape == "integer":
        return (None if _INTEGER_ANSWER.fullmatch(candidate) else
                "Return only one integer, without a preamble, Markdown or a decimal fraction.")
    if shape == "digits":
        return (
            None
            if re.fullmatch(r"[0-9]+", candidate)
            else "Return only the requested digits, without a preamble or Markdown."
        )
    if shape == "json_boolean":
        return (
            None
            if candidate in {"true", "false"}
            else "Return only a JSON boolean literal: true or false, without Markdown."
        )
    if shape in {"json_object", "json_array"}:

        def reject_constant(_value):
            raise ValueError("non-JSON numeric constant")

        try:
            parsed = json.loads(candidate, parse_constant=reject_constant)
            if isinstance(parsed, dict if shape == "json_object" else list):
                return None
        except (ValueError, RecursionError):
            pass
        return (
            "Return only one valid JSON "
            + ("object" if shape == "json_object" else "array")
            + ", without a preamble, Markdown fence or trailing explanation."
        )
    if shape == "python_list":
        # Parse a literal, never execute generated Python. The length/node
        # bounds also apply to rejected proposals before recursive evaluation.
        try:
            if len(candidate) <= 65536:
                expression = ast.parse(candidate, mode="eval")
                if (
                    isinstance(expression.body, ast.List)
                    and ast.get_source_segment(candidate, expression.body) == candidate
                    and sum(1 for _node in ast.walk(expression)) <= 4096
                    and isinstance(ast.literal_eval(expression), list)
                ):
                    return None
        except (SyntaxError, ValueError, RecursionError):
            pass
        return (
            "Return only one Python list literal, without Markdown, "
            "an assignment, a computed expression or trailing explanation."
        )
    if (
        not candidate
        or len(candidate.splitlines()) != 1
        or candidate.startswith((">", "#", "-", "*", "`"))
        or "**" in candidate
        or "`" in candidate
    ):
        return "Return only the requested sentence on one plain-text line."
    return None


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class _WebEntryBindingError(ValueError):
    """The host cannot bind changed web files to one delivered page."""


def _page_references_asset(page: Path, asset: Path, root: Path) -> bool:
    """Allow root pages for nested assets only with an explicit local reference."""
    class References(HTMLParser):
        paths: list[str]
        has_base = False

        def __init__(self):
            super().__init__()
            self.paths = []

        def handle_starttag(self, tag, attrs):
            if tag == "base":
                self.has_base = True
            key = "src" if tag == "script" else "href" if tag == "link" else None
            value = dict(attrs).get(key) if key else None
            if value:
                self.paths.append(value)

    try:
        parser = References()
        with page.open(encoding="utf-8") as stream:
            parser.feed(stream.read(1024 * 1024))
        if parser.has_base:
            return False
        for value in parser.paths:
            url = urlsplit(value)
            if url.scheme or url.netloc or not url.path:
                continue
            path = unquote(url.path)
            base = root if path.startswith("/") else page.parent
            if (base / path.lstrip("/")).resolve() == asset:
                return True
    except (OSError, ValueError):
        pass
    return False


def _written_web_entry(store: RunStore, scope: RunScope, run_id: str, root: Path) -> str | None:
    """Bind observed changes to their page, never an unrelated root fallback.

    A continued Run may change only JS/CSS in an existing subproject. Use
    its nearest entry directory and any host-recorded page in that directory.
    Ambiguous or unavailable bindings are host blockers, not game defects.
    """
    try:
        snapshot = store.load(scope, run_id)
    except Exception:
        return None
    proposed: dict[str, str] = {}
    written: set[str] = {
        path
        for path in snapshot.workspace_modified_paths
        if path.endswith((".html", ".htm", ".js", ".jsx", ".css"))
    }
    for message in snapshot.messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict) or function.get("name") != "write_workspace_file":
                    continue
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        continue
                path = arguments.get("path") if isinstance(arguments, dict) else None
                call_id = call.get("id")
                if (
                    isinstance(call_id, str)
                    and isinstance(path, str)
                    and path.endswith((".html", ".htm", ".js", ".jsx", ".css"))
                ):
                    proposed[call_id] = path
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in proposed:
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = next(
                    (
                        block.get("text")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ),
                    None,
                )
            try:
                receipt = json.loads(content) if isinstance(content, str) else None
            except ValueError:
                receipt = None
            if isinstance(receipt, dict) and receipt.get("status") == "ok":
                written.add(proposed[call_id])
    if not written:
        return None
    resolved_root = root.resolve()
    observation = getattr(snapshot, "last_browser_observation", None)
    observed_path = observation.get("entry_path") if isinstance(observation, dict) else None
    observed = (resolved_root / observed_path).resolve() if isinstance(observed_path, str) else None
    if observed is not None and not (
        observed.is_relative_to(resolved_root)
        and observed.suffix in {".html", ".htm"}
        and observed.is_file()
    ):
        observed = None
    candidates: set[Path] = set()
    for path in written:
        target = (resolved_root / path).resolve()
        if not target.is_relative_to(resolved_root):
            raise _WebEntryBindingError(
                "Web entry binding blocked: changed files leave the workspace."
            )
        if target.suffix in {".html", ".htm"}:
            if not target.is_file():
                raise _WebEntryBindingError(
                    "Web entry binding blocked: the changed page is unavailable."
                )
            candidates.add(target)
            continue
        directory = target.parent
        # A nested project with no known entry must not inherit an unrelated
        # old page at workspace root. Root assets can still bind a root page.
        while directory.is_relative_to(resolved_root):
            local_project = directory != resolved_root or target.parent == resolved_root
            # The observation is a host receipt, not a path asserted in final text.
            if observed is not None and observed.parent == directory and (
                local_project or _page_references_asset(observed, target, resolved_root)
            ):
                candidates.add(observed)
                break
            entry = (directory / "index.html").resolve()
            if entry.is_relative_to(resolved_root) and entry.is_file() and (
                local_project or _page_references_asset(entry, target, resolved_root)
            ):
                candidates.add(entry)
                break
            directory = directory.parent
        else:
            raise _WebEntryBindingError(
                "Web entry binding blocked: no page is bound to the changed web files. "
                "Inspect the intended project entry with the browser tool before verifying."
            )
    if len(candidates) != 1:
        raise _WebEntryBindingError(
            "Web entry binding blocked: changed files belong to multiple pages. "
            "The host must bind the intended delivery entry before verifying; "
            "this is not evidence of a defect in the delivered page."
        )
    return next(iter(candidates)).relative_to(resolved_root).as_posix()


class StudioFinalShapeVerifier:
    """Per-activation verifier with no authority to approve task semantics."""

    def __init__(
        self,
        scope: RunScope,
        *,
        build_id: str,
        workspace_root: Path | None = None,
        store: RunStore | None = None,
        browser: StudioNativeBrowserPort | None = None,
        require_visual_feedback: bool = False,
    ) -> None:
        self._scope = scope.model_copy(deep=True)
        self._environment_digest = _sha256(
            f"{build_id}\0{VERIFIER_REF}\0{VERIFIER_VERSION}\0"
            f"{BROWSER_VERIFIER_REF}\0{BROWSER_VERIFIER_VERSION}\0"
            f"{ARTIFACT_VERIFIER_REF}\0{ARTIFACT_VERIFIER_VERSION}\0"
            f"visual-feedback:{int(require_visual_feedback)}\0web-entry-binding:2"
        )
        self._workspace_root = workspace_root
        self._store = store
        self._browser = browser
        self._require_visual_feedback = require_visual_feedback

    async def verify(self, request: VerificationRequest) -> VerificationBundle:
        if request.scope != self._scope:
            raise ValueError("native_final_shape_scope_mismatch")
        acceptance = request.contract.items[0] if request.contract.items else None
        general_evidence = (
            acceptance is not None
            and acceptance.verifier_ref == EVIDENCE_VERIFIER_REF
            and acceptance.verifier_version == EVIDENCE_VERIFIER_VERSION
        )
        candidate = CompletionCandidate(
            candidate_id=request.candidate_id,
            final_text=request.final_text,
            subject_digest=_sha256(request.final_text),
            subject_context_digest=request.subject_context_digest,
            # Do not reinterpret legacy persisted reports under new semantics.
            environment_digest=_sha256(
                f"{self._environment_digest}\0{EVIDENCE_VERIFIER_REF}\0"
                f"{EVIDENCE_VERIFIER_VERSION}"
            ) if general_evidence else self._environment_digest,
        )
        if not 1 <= len(request.contract.items) <= 2:
            return VerificationBundle(candidate=candidate, report=None)
        acceptance = request.contract.items[0]
        shape = requested_final_shape(acceptance.description)
        browser_check = (
            (
                general_evidence
                or (
                    acceptance.verifier_ref == BROWSER_VERIFIER_REF
                    and acceptance.verifier_version == BROWSER_VERIFIER_VERSION
                )
            )
            and self._browser is not None
            and self._store is not None
            and self._workspace_root is not None
        )
        artifact_item = request.contract.items[1] if len(request.contract.items) == 2 else None
        exact_file = (
            _decode_exact_workspace_file(artifact_item.description) if artifact_item else None
        )
        if (
            (shape is None and exact_file is None and not browser_check and not general_evidence)
            or (
                not general_evidence
                and not browser_check
                and (
                    acceptance.verifier_ref != VERIFIER_REF
                    or acceptance.verifier_version != VERIFIER_VERSION
                )
            )
            or acceptance.verification_method != "deterministic"
            or (
                artifact_item is not None
                and (
                    exact_file is None
                    or self._workspace_root is None
                    or artifact_item.acceptance_id != "exact_workspace_file"
                    or artifact_item.verifier_ref != ARTIFACT_VERIFIER_REF
                    or artifact_item.verifier_version != ARTIFACT_VERIFIER_VERSION
                    or artifact_item.verification_method != "deterministic"
                )
            )
        ):
            return VerificationBundle(candidate=candidate, report=None)
        shape_failure = (
            _violation(shape, request.final_text) if shape and request.purpose == "final" else None
        )
        browser_failure = None
        browser_checked = False
        browser_controls = ""
        browser_evidence_ref: str | None = None
        browser_blocker = None
        if browser_check:
            assert self._store is not None and self._workspace_root is not None
            try:
                entry = _written_web_entry(
                    self._store, self._scope, request.run_id, self._workspace_root
                )
            except _WebEntryBindingError as exc:
                entry = None
                browser_blocker = str(exc)
                browser_failure = browser_blocker
                browser_evidence_ref = _sha256(browser_blocker)
            if entry is not None:
                try:
                    observation = await self._browser.verify_page(
                        run_id=request.run_id,
                        entry_path=entry,
                        candidate_id=request.candidate_id,
                    )
                    browser_checked = True
                    browser_evidence_ref = (
                        observation.screenshot.content_ref
                        if observation.screenshot is not None
                        else _sha256(
                            json.dumps(
                                {
                                    "entry": entry,
                                    "status": observation.status,
                                    "status_code": observation.status_code,
                                    "error_code": getattr(observation, "error_code", None),
                                    "console": observation.console,
                                    "page_errors": observation.page_errors,
                                    "page_error_locations": getattr(
                                        observation, "page_error_locations", ()
                                    ),
                                    "network_failures": observation.network_failures,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        )
                    )
                    browser_controls = ", ".join(
                        f"{control.label[:80]} ({control.selector or control.tag})"
                        for control in observation.controls[:8]
                        if control.label
                    )[:800]
                    if observation.status != "ok" or (
                        observation.status_code is not None and observation.status_code >= 400
                    ):
                        reason = (
                            getattr(observation, "error_code", None)
                            or observation.status_code or observation.status
                        )
                        browser_failure = (
                            "The delivered page has multiple visible start controls. "
                            "Remove duplicate launch controls and inspect the current page again."
                            if reason == "browser_launch_control_ambiguous"
                            else f"Browser could not load the delivered page: {reason}."
                        )
                        # An incomplete browser operation does not establish a
                        # defect in the delivery. Only an observed HTTP failure
                        # or duplicate launch controls can request a code repair.
                        if reason != "browser_launch_control_ambiguous" and not (
                            observation.status_code is not None and observation.status_code >= 400
                        ):
                            browser_blocker = (
                                "Browser verification is blocked by an incomplete host "
                                "observation. Restore the browser and verify the same page; "
                                "no delivery defect has been established."
                            )
                            browser_failure = browser_blocker
                    elif (
                        any(
                            not browser_environment_warning(item)
                            for item in observation.page_errors
                        )
                        or any(
                            item.startswith(("error:", "assert:"))
                            and not browser_environment_warning(item)
                            for item in observation.console
                        )
                        or observation.network_failures
                    ):
                        policy_denial = next(
                            (
                                item
                                for item in observation.network_failures
                                if item.startswith(("network_denied:", "websocket_denied:"))
                            ),
                            None,
                        )
                        if policy_denial is not None:
                            browser_failure = (
                                "Browser blocked a page request under the locked network "
                                "policy. Use assets served from the authorized local origin "
                                "and inspect the page again."
                            )
                        else:
                            missing_asset = next(
                                (
                                    item
                                    for item in observation.network_failures
                                    if item.startswith("asset_http_error:")
                                ),
                                None,
                            )
                            if missing_asset is not None:
                                # Chromium's generic console 404 lacks the
                                # requested path. The browser host recorded
                                # the failing local script/style response.
                                browser_failure = (
                                    "Browser could not load a local script or stylesheet: "
                                    f"{missing_asset[:300]}. Fix that asset and inspect again."
                                )
                            else:
                                error = (
                                    *(
                                        item
                                        for item in observation.page_errors
                                        if not browser_environment_warning(item)
                                    ),
                                    *(
                                        item
                                        for item in observation.console
                                        if item.startswith(("error:", "assert:"))
                                        and not browser_environment_warning(item)
                                    ),
                                    *observation.network_failures,
                                )[0]
                                locations = getattr(observation, "page_error_locations", ())
                                where = f" at {locations[0][:300]}" if locations else ""
                                browser_failure = (
                                    f"Browser reported an error in {entry}{where}: {error[:500]}"
                                )
                except Exception:
                    # An unavailable host browser is missing evidence, not
                    # proof that the user's implementation is defective.
                    browser_blocker = (
                        "Browser verification is unavailable. Restore the host browser "
                        "or evidence service and verify the same page; do not modify "
                        "the delivery solely to clear this blocker."
                    )
                    browser_failure = browser_blocker
                    browser_evidence_ref = _sha256(f"browser_unavailable\0{entry}")
            # Checkpoints are triggered after a browser observation on changed
            # web files. Surface a wrong/stale delivery page immediately, while
            # the model still has tool budget to inspect and repair it.
            if (
                request.purpose in {"final", "checkpoint"}
                and entry is not None
                and browser_blocker is None
            ):
                try:
                    run_snapshot = self._store.load(self._scope, request.run_id)
                    unresolved = run_snapshot.unresolved_interaction_observation
                except Exception:
                    run_snapshot = None
                    unresolved = None
                if (
                    isinstance(unresolved, dict)
                    and unresolved.get("tool_name") == "browser_inspect_workspace"
                    and unresolved.get("entry_path") == entry
                ):
                    browser_failure = (
                        "The browser interaction previously failed and has not been "
                        "successfully rechecked: "
                        + str(unresolved.get("error") or "browser error")[:300]
                    )
                    ref = unresolved.get("evidence_ref")
                    if isinstance(ref, str):
                        browser_evidence_ref = ref
                if browser_failure is None:
                    observed = (
                        getattr(run_snapshot, "last_browser_observation", None)
                        if run_snapshot is not None
                        else None
                    )
                    last_web_change = (
                        getattr(run_snapshot, "last_web_mutation_tool_call", None)
                        if run_snapshot is not None
                        else None
                    )
                    fresh = (
                        isinstance(observed, dict)
                        and observed.get("entry_path") == entry
                        and type(observed.get("tool_call")) is int
                        and observed["tool_call"] >= (last_web_change or 0)
                        and (
                            not self._require_visual_feedback
                            or observed.get("vision_description_status") == "available"
                        )
                    )
                    if not fresh:
                        browser_failure = (
                            "The current screenshot has no usable visual-model description. "
                            "Inspect the latest page again with the authorized browser tool."
                            if self._require_visual_feedback
                            and isinstance(observed, dict)
                            and observed.get("entry_path") == entry
                            and type(observed.get("tool_call")) is int
                            and observed["tool_call"] >= (last_web_change or 0)
                            else "The delivered web page has not been observed through the "
                            "browser tool after the latest web file change. Open and inspect "
                            "the current page before finalizing."
                        )
                        browser_evidence_ref = _sha256(
                            f"browser_observation_missing\0{entry}\0{last_web_change}"
                        )
        if (
            request.purpose == "checkpoint"
            and not browser_checked
            and exact_file is None
            and browser_blocker is None
        ):
            return VerificationBundle(candidate=candidate, report=None)
        artifact_ok = False
        artifact_observation = ""
        if exact_file is not None and self._workspace_root is not None:
            artifact_ok, artifact_observation = _read_exact_workspace_file(
                self._workspace_root, exact_file
            )
        failure = (
            shape_failure
            or browser_failure
            or (artifact_observation if exact_file and not artifact_ok else None)
        )
        items = [
            VerificationItemResult(
                acceptance_id=acceptance.acceptance_id,
                status="blocked" if browser_blocker else "failed" if failure else "not_run",
                verifier_id=acceptance.verifier_ref,
                verifier_version=acceptance.verifier_version,
                evidence_refs=(browser_evidence_ref,)
                if browser_failure and browser_evidence_ref is not None
                else (_sha256(f"{request.final_text}\0{artifact_observation}"),)
                if failure
                else (),
                verification_method="deterministic",
                safe_summary=browser_blocker or failure
                or (
                    "Browser smoke check observed no load or script errors. "
                    f"Visible controls: {browser_controls or 'none'}. "
                    "Interaction and task semantics were not verified."
                    if browser_checked
                    else (
                        "Final-answer shape passed; task semantics were not independently verified."
                    )
                    if shape
                    else "Task semantics were not independently verified."
                    if general_evidence
                    else "No independent browser evidence was available."
                ),
            )
        ]
        if artifact_item is not None:
            items.append(
                VerificationItemResult(
                    acceptance_id="exact_workspace_file",
                    status="passed" if artifact_ok else "failed",
                    verifier_id=ARTIFACT_VERIFIER_REF,
                    verifier_version=ARTIFACT_VERIFIER_VERSION,
                    evidence_refs=("sha256:" + artifact_observation,) if artifact_ok else (),
                    verification_method="deterministic",
                    safe_summary=(
                        "Requested file bytes match exactly."
                        if artifact_ok
                        else artifact_observation
                    ),
                )
            )
        report = VerificationReport(
            candidate_id=request.candidate_id,
            contract_revision=request.contract.revision,
            subject_digest=candidate.subject_digest,
            subject_context_digest=request.subject_context_digest,
            environment_digest=candidate.environment_digest,
            items=tuple(items),
            repairable=bool(failure) and browser_blocker is None,
            failure_fingerprint=(
                _sha256(f"{shape}\0{request.final_text}\0{artifact_observation}\0{browser_failure}")
                if failure
                else ""
            ),
        )
        return VerificationBundle(candidate=candidate, report=report)
