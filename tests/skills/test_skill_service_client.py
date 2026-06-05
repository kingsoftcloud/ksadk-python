from __future__ import annotations

from ksadk.skills.models import SkillListResponse


def test_parse_list_skills_by_space_id_preserves_progressive_disclosure_fields():
    response = SkillListResponse.from_payload(
        {
            "Code": 0,
            "Message": "OK",
            "RequestId": "req-1",
            "Data": {
                "SkillSpaceId": "ss-abc",
                "SkillSpaceName": "office",
                "Skills": [
                    {
                        "SkillId": "sk-web",
                        "VersionId": "sv-web-v1",
                        "Version": "v1",
                        "Name": "web-artifacts-builder",
                        "Description": "Build web artifacts",
                        "Status": "Active",
                        "ContentHash": "sha256:b95f0735357fcf879bd53ed85cb242679ec74438e3bc8e85b1f27193169b6ecf",
                        "ArchiveUri": "ks3://agentengine-skills/skills/sk-web/v1/web-artifacts-builder.zip",
                    }
                ],
            },
        }
    )

    assert response.request_id == "req-1"
    assert response.space_id == "ss-abc"
    assert response.skills[0].skill_id == "sk-web"
    assert response.skills[0].version_id == "sv-web-v1"
    assert response.skills[0].version == "v1"
    assert response.skills[0].content_hash.algorithm == "sha256"
    assert response.skills[0].archive_uri == "ks3://agentengine-skills/skills/sk-web/v1/web-artifacts-builder.zip"


def test_parse_list_skills_preserves_discovery_metadata():
    response = SkillListResponse.from_payload(
        {
            "Data": {
                "SkillSpaceId": "ss-abc",
                "Skills": [
                    {
                        "SkillId": "sk-report",
                        "VersionId": "sv-report-v1",
                        "Name": "report-writer",
                        "Description": "Write research reports",
                        "Aliases": ["研究报告", "deep report"],
                        "Tags": ["writing", "research"],
                        "Examples": ["生成一份行业研究报告"],
                        "InputSchema": {"type": "object"},
                        "RuntimeRequirements": {"sandbox": True},
                    }
                ],
            }
        }
    )

    skill = response.skills[0]
    assert skill.aliases == ("研究报告", "deep report")
    assert skill.tags == ("writing", "research")
    assert skill.examples == ("生成一份行业研究报告",)
    assert skill.input_schema == {"type": "object"}
    assert skill.runtime_requirements == {"sandbox": True}


def test_parse_list_skills_filters_inactive_by_default():
    response = SkillListResponse.from_payload(
        {
            "Data": {
                "SkillSpaceId": "ss-abc",
                "Skills": [
                    {"SkillId": "sk-active", "VersionId": "v1", "Version": "1", "Name": "active", "Status": "Active"},
                    {
                        "SkillId": "sk-available",
                        "VersionId": "v1",
                        "Version": "1",
                        "Name": "available",
                        "Status": "AVAILABLE",
                    },
                    {"SkillId": "sk-disabled", "VersionId": "v2", "Version": "2", "Name": "disabled", "Status": "Disabled"},
                ],
            }
        }
    )

    assert [skill.skill_id for skill in response.active_skills()] == ["sk-active", "sk-available"]
