from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ksadk.harness import HarnessConfig, HarnessReasoningTurn, HarnessRuntimeAdapter
from ksadk.runtime import RuntimeExecutor, RuntimeRegistry
from ksadk.studio.contracts import AgentSpec, RunStatus
from ksadk.studio.scheduler_assistant import ScheduleIntent, StudioScheduleAssistant
from ksadk.studio.service import StudioService


class Model:
    def __init__(self):
        self.intent = {
            "action": "create",
            "display_name": "日报",
            "prompt": "生成昨日工作日报",
            "schedule": {"kind": "cron", "expression": "0 10 * * *", "timezone": "Asia/Shanghai"},
        }
        self.requests = []

    async def complete(self, model, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            tool_calls=[SimpleNamespace(name="studio_schedule", arguments=json.dumps(self.intent))]
        )


class FixtureAdapter(HarnessRuntimeAdapter):
    async def start(self, request):
        handle = await super().start(request)
        return handle.model_copy(update={"runtime_type": "adk"})

    def _require_run(self, handle):
        return super()._require_run(handle.model_copy(update={"runtime_type": "harness"}))


class Reasoner:
    async def complete(self, **kwargs):
        return HarnessReasoningTurn(
            final_text="执行结果：" + str(kwargs["messages"][-1]["content"]), tool_calls=()
        )


@pytest.fixture
def studio(tmp_path):
    model = Model()
    registry = RuntimeRegistry()
    registry.register(
        "adk",
        lambda context: FixtureAdapter(
            HarnessConfig(model="test-model", prompt="Test"), reasoner=Reasoner()
        ),
    )
    service = StudioService(
        tmp_path, runtime_executor=RuntimeExecutor(registry), model_client=model
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "agent.py").write_text(
        "from google.adk.agents import Agent\n"
        "root_agent = Agent(name='assistant', model='test-model')\n"
    )
    spec = AgentSpec.model_validate(
        {
            "runtime": {
                "type": "adk",
                "version": "test",
                "projectPath": "project",
                "entryPoint": "agent:root_agent",
            },
            "instructions": {"system": "Test"},
            "model": {
                "provider": "openai-compatible",
                "model": "test-model",
                "endpointUrl": "https://model.example.com/v1/chat/completions",
                "credentialRef": "env://MODEL_API_KEY",
            },
            "security": {"network": {"mode": "restricted", "allowedHosts": ["model.example.com"]}},
        }
    )
    service.create_studio_agent(agent_id="test-agent", name="Test", spec=spec, runtime=spec.runtime)
    return service, model


@pytest.mark.asyncio
async def test_chat_creates_durable_schedule_and_replays_tool_receipt(studio):
    service, model = studio
    build = await service.ensure_current_build("test-agent")
    spec = service.resolve_run_spec(build.id)
    try:
        run = await service.run_service.run(
            spec, "每天10点帮我生成昨日工作日报", session_id="chat-1"
        )
        assert run.status == RunStatus.COMPLETED, run.error
        (task,) = service.list_agent_schedules("test-agent")
        assert task.schedule.expression == "0 10 * * *"
        assert task.target.agent_version_ref == build.id
        assert task.command.payload["content"] == "生成昨日工作日报"
        assert "已创建定时任务" in run.output
        rows = await service.session_service.get_events("chat-1")
        serialized = json.dumps([asdict(row) for row in rows], ensure_ascii=False, default=str)
        assert task.task_id in serialized
        assert "studio_schedule" in serialized
        assert "10:00" in run.output

        # Force a real due time and tick the production Inbox/Kernel worker.
        due = datetime.now(timezone.utc) - timedelta(seconds=1)
        once = task.model_copy(
            update={
                "schedule": task.schedule.model_copy(
                    update={
                        "kind": "once",
                        "expression": None,
                        "at": due,
                        "misfire_policy": "run_once",
                    }
                ),
                "next_run_at": due,
            }
        )
        service.scheduler.store.put_task(once, generation=2)
        await service.scheduler.tick()
        import asyncio

        for _ in range(80):
            await asyncio.sleep(0.05)
            await service.scheduler.tick()
            occurrences = service.scheduler.list_occurrences(task.task_id)
            if occurrences and occurrences[0].state == "succeeded":
                break
        assert occurrences[0].state == "succeeded", occurrences
        assert occurrences[0].session_id != "chat-1"
        assert len(model.requests) == 1
    finally:
        await service.scheduler.stop()
        await service.aclose()


@pytest.mark.asyncio
async def test_interval_pause_resume_and_cross_agent_target(studio):
    service, model = studio
    assistant = service.run_service.schedule_assistant
    model.intent["schedule"] = {
        "kind": "interval",
        "everySeconds": 420,
        "timezone": "Asia/Shanghai",
    }
    build = await service.ensure_current_build("test-agent")
    try:
        run = await service.run_service.run(
            service.resolve_run_spec(build.id), "每7分钟帮我生成日报", session_id="chat-1"
        )
        assert run.status == RunStatus.COMPLETED, run.error
        (task,) = service.list_agent_schedules("test-agent")
        assert task.schedule.every_seconds == 420
        for action, enabled in (("pause", False), ("resume", True)):
            await assistant.execute(
                "test-agent", "chat-1", ScheduleIntent(action=action, task_id=task.task_id)
            )
            assert service.scheduler.get_task(task.task_id).enabled is enabled
        with pytest.raises(Exception, match="不存在"):
            await assistant.execute(
                "another-agent", "chat-1", ScheduleIntent(action="delete", task_id=task.task_id)
            )
        assert service.scheduler.get_task(task.task_id)
    finally:
        await service.scheduler.stop()
        await service.aclose()


@pytest.mark.asyncio
async def test_discussion_and_incomplete_request_do_not_create(studio):
    service, model = studio
    build = await service.ensure_current_build("test-agent")
    try:
        model.intent = {"action": "ignore"}
        run = await service.run_service.run(
            service.resolve_run_spec(build.id), "解释每天10点的Cron怎么写"
        )
        assert run.status == RunStatus.COMPLETED, run.error
        assert "执行结果" in run.output
        assert not service.scheduler.list_tasks()
        model.intent = {"action": "clarify", "question": "每天几点执行？"}
        run = await service.run_service.run(service.resolve_run_spec(build.id), "每天帮我生成日报")
        assert run.output == "每天几点执行？"
        assert not service.scheduler.list_tasks()
        model.intent = {"action": "create", "prompt": "报告"}
        run = await service.run_service.run(service.resolve_run_spec(build.id), "创建定时任务")
        assert run.status == RunStatus.FAILED
        assert not service.scheduler.list_tasks()
    finally:
        await service.scheduler.stop()
        await service.aclose()


@pytest.mark.asyncio
async def test_schedule_followup_and_cancel_before_tool_execution(studio):
    import asyncio

    service, model = studio
    build = await service.ensure_current_build("test-agent")
    spec = service.resolve_run_spec(build.id)
    try:
        model.intent = {"action": "clarify", "question": "每天几点执行？"}
        await service.run_service.run(spec, "每天帮我生成日报", session_id="chat-followup")
        assert service.run_service.schedule_assistant.matches_followup(
            "test-agent", "chat-followup", "上午10点"
        )
        assert not service.run_service.schedule_assistant.matches_followup(
            "test-agent", "another-session", "上午10点"
        )
        assert not service.run_service.schedule_assistant.matches_followup(
            "test-agent", "chat-followup", "介绍Python"
        )
        entered, release = asyncio.Event(), asyncio.Event()
        original = model.complete

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        model.complete = delayed
        model.intent = {
            "action": "create",
            "prompt": "日报",
            "schedule": {"kind": "interval", "everySeconds": 300},
        }
        pending = asyncio.create_task(
            service.run_service.run(spec, "每5分钟生成日报", session_id="chat-cancel")
        )
        await entered.wait()
        run = service.event_store.list_runs(session_id="chat-cancel")[0]
        await service.run_service.cancel_run(run.id)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not service.scheduler.list_tasks()
        assert service.event_store.get(run.id).status == RunStatus.CANCELLED
    finally:
        await service.scheduler.stop()
        await service.aclose()


@pytest.mark.asyncio
async def test_continuation_rejects_a_session_owned_by_another_agent(studio):
    service, _ = studio
    await service.session_service.create_session("another-agent", "local-user", "foreign-session")
    try:
        with pytest.raises(Exception, match="该智能体已有的会话"):
            await service.run_service.schedule_assistant.execute(
                "test-agent",
                "foreign-session",
                ScheduleIntent(
                    action="create",
                    prompt="报告",
                    continuity="continue_session",
                    schedule={"kind": "interval", "everySeconds": 300},
                ),
            )
        assert not service.scheduler.list_tasks()
    finally:
        await service.scheduler.stop()
        await service.aclose()


@pytest.mark.parametrize(
    "text",
    [
        "每天10点帮我总结",
        "每7分钟检查服务",
        "每半小时检查",
        "schedule a daily report",
        "每隔多少分钟执行",
        "明天下午3点提醒我",
    ],
)
def test_schedule_intent_candidates(text):
    assert StudioScheduleAssistant.matches(text)


def test_general_chat_does_not_require_scheduler_model():
    assert not StudioScheduleAssistant.matches("介绍一下你自己")
