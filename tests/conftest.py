"""全局测试配置。

## 浏览器 E2E 默认不跑

`tests/native/` 和 `tests/studio/` 里有 playwright 用例会**启动真实浏览器**、
**接管鼠标和键盘**。本地跑全量套件时它们会抢焦点，严重影响正常工作。

需要跑浏览器用例时显式开启：

    KSADK_BROWSER_TESTS=1 pytest tests/

## 为什么用显式列表

`collect_ignore_glob` 在这种带 `/` 的相对路径上不可靠（实测漏掉了
`test_coordinate_observer_browser_live.py`）。显式文件列表最稳。
"""

from __future__ import annotations

import os

BROWSER_TESTS_ENABLED = os.environ.get("KSADK_BROWSER_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
}

# 所有 import playwright 的测试文件（54 个）
BROWSER_TEST_FILES = [
    'architecture/test_python_module_size.py',
    'conftest.py',
    'e2e/check_submitted_input_browser.py',
    'e2e/test_a2ui_version_compatibility.py',
    'harness/native/test_browser_context.py',
    'harness/native/test_browser_element_refs.py',
    'harness/native/test_browser_observation_capture.py',
    'harness/native/test_browser_scroll_protocol.py',
    'harness/native/test_browser_session.py',
    'harness/native/test_browser_vision_adapter.py',
    'harness/native/test_live_browser_task.py',
    'harness/native/test_live_browser_vision.py',
    'harness/native/test_tool_feedback.py',
    'harness/native/test_tool_schema_enum_types.py',
    'harness/native/test_tool_schema_projection.py',
    'native/game_phase2_browser_inspect.py',
    'native/game_phase2_interaction_acceptance.py',
    'native/game_phase2_world_action_acceptance.py',
    'native/phase0_reconstructed_extended.py',
    'native/phase2_live_matrix.py',
    'native/test_coordinate_observer_browser_live.py',
    'native/test_game_phase2_interaction_acceptance.py',
    'native/test_game_phase2_world_action_acceptance.py',
    'native/test_game_player_path_calibration.py',
    'native/test_inventory_observation.py',
    'native/test_visible_inventory_quantity.py',
    'packaging/test_release_preflight.py',
    'studio/e2e/agent_policy_editor_browser_e2e.py',
    'studio/e2e/channel_workspace_browser_e2e.py',
    'studio/e2e/conversation_items_browser_e2e.py',
    'studio/e2e/conversation_reconnect_browser_e2e.py',
    'studio/e2e/harness_authoring_browser_e2e.py',
    'studio/e2e/native_child_controls_browser_e2e.py',
    'studio/e2e/native_controls_browser_e2e.py',
    'studio/e2e/native_durable_controls_browser_e2e.py',
    'studio/e2e/native_durable_session_browser_e2e.py',
    'studio/e2e/pcm_browser_smoke.py',
    'studio/e2e/scheduler_browser_e2e.py',
    'studio/e2e/scheduler_fault_matrix_browser_e2e.py',
    'studio/e2e/scheduler_harness_browser_e2e.py',
    'studio/e2e/studio_browser_smoke.py',
    'studio/e2e/studio_creation_layout_smoke.py',
    'studio/e2e/studio_layout_smoke.py',
    'studio/e2e/studio_resource_forms_smoke.py',
    'studio/e2e/studio_responsive_smoke.py',
    'studio/e2e/studio_style_comparison.py',
    'studio/e2e/studio_visual_review.py',
    'studio/e2e/studio_workflow_layout_smoke.py',
    'studio/e2e/studio_workspace_navigation_smoke.py',
    'studio/test_native_browser_startup_failure.py',
    'studio/test_native_tool_schema_enum_types.py',
    'studio/test_native_tool_schema_projection.py',
    'studio/test_pcm_e2e.py',
    'test_openai_protocol_e2e.py',
]

collect_ignore = [] if BROWSER_TESTS_ENABLED else list(BROWSER_TEST_FILES)
