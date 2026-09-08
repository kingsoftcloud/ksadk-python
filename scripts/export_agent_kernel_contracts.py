"""导出 Agent Kernel v1 合同 manifest。

对 contracts/agent-kernel/v1 下的 schema 和 fixtures 做 UTF-8、key sort、LF 规范化，
按相对路径排序计算 SHA-256 aggregate digest，输出 manifest.json。
--check 只校验磁盘内容与 manifest 一致，不重写文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_DIR = REPO_ROOT / "contracts" / "agent-kernel" / "v1"


def canonical_bytes(data) -> bytes:
    # key sort + 无多余空白 + UTF-8。
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def iter_contract_files(contract_dir: Path) -> list[Path]:
    return sorted(
        p for p in contract_dir.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    )


def build_manifest(contract_dir: Path, *, contract_set: str = "agent-kernel/v1") -> dict:
    aggregate = hashlib.sha256()
    files = []
    for path in iter_contract_files(contract_dir):
        canonical = canonical_bytes(json.loads(path.read_text(encoding="utf-8")))
        rel = path.relative_to(contract_dir).as_posix()
        digest = hashlib.sha256(canonical).hexdigest()
        files.append({"path": rel, "sha256": digest, "bytes": len(canonical)})
        aggregate.update(rel.encode("utf-8") + b"\0" + canonical)
    return {
        "contract_set": contract_set,
        "digest_algorithm": "sha256",
        "canonicalization": "utf-8; json key sort; no whitespace; lf; path-sorted",
        "aggregate_digest": aggregate.hexdigest(),
        "files": files,
    }


def contract_digest(contract_dir: Path) -> str:
    aggregate = hashlib.sha256()
    for path in iter_contract_files(contract_dir):
        canonical = canonical_bytes(json.loads(path.read_text(encoding="utf-8")))
        aggregate.update(path.relative_to(contract_dir).as_posix().encode("utf-8") + b"\0" + canonical)
    return aggregate.hexdigest()


def check(contract_dir: Path, *, contract_set: str = "agent-kernel/v1") -> int:
    manifest_path = contract_dir / "manifest.json"
    if not manifest_path.exists():
        print("manifest.json missing; run without --check to generate", file=sys.stderr)
        return 1
    current = build_manifest(contract_dir, contract_set=contract_set)
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if current != recorded:
        print("manifest out of date: contract files changed", file=sys.stderr)
        return 1
    print(f"contract_set={recorded['contract_set']}")
    print(f"aggregate_digest={recorded['aggregate_digest']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="校验磁盘内容与 manifest 一致")
    parser.add_argument("--contract-dir", type=Path, default=CONTRACT_DIR)
    parser.add_argument(
        "--contract-set",
        default="agent-kernel/v1",
        help="写入 manifest 的版本化合同集合标识",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check(args.contract_dir, contract_set=args.contract_set)

    manifest = build_manifest(args.contract_dir, contract_set=args.contract_set)
    (args.contract_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"contract_set={manifest['contract_set']}")
    print(f"aggregate_digest={manifest['aggregate_digest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
