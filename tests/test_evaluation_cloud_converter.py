from __future__ import annotations

import pytest

from ksadk.evaluation.cloud_converter import (
    EvalSetCloudConversionError,
    evalset_from_dataset_snapshot,
    evalset_to_dataset_snapshot,
)
from ksadk.evaluation.evalset import parse_evalset


def _evalset():
    return parse_evalset(
        {
            "schemaVersion": "ksadk.eval/v1",
            "name": "support-regression",
            "metadata": {"owner": "qa"},
            "cases": [
                {
                    "id": "case-001",
                    "turns": [
                        {"input": "如何修改密码？", "expectedOutput": "设置"},
                        {"input": "忘记旧密码怎么办？"},
                    ],
                    "assertions": [{"type": "response.contains", "value": "密码"}],
                    "metadata": {"priority": "p0"},
                }
            ],
        }
    )


def test_evalset_snapshot_round_trip_preserves_multi_turn_cases_and_digest():
    original = _evalset()

    snapshot = evalset_to_dataset_snapshot(original)
    restored = evalset_from_dataset_snapshot(snapshot)

    assert [column.name for column in snapshot.columns] == [
        "case_id",
        "turns",
        "assertions",
        "case_metadata",
        "source_format",
        "ksadk_content_digest",
    ]
    assert snapshot.rows[0].values["turns"][1]["input"] == "忘记旧密码怎么办？"
    assert restored.content_digest == original.content_digest
    assert restored.model_dump(mode="json") == original.model_dump(mode="json")


def test_snapshot_rejects_rows_that_do_not_match_the_fixed_schema():
    snapshot = evalset_to_dataset_snapshot(_evalset())
    snapshot.rows[0].values.pop("turns")

    with pytest.raises(EvalSetCloudConversionError, match="turns"):
        evalset_from_dataset_snapshot(snapshot)

