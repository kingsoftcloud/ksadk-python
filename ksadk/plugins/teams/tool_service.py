"""Shared trusted Teams tool schemas and domain invocation.

Hosts provide a verified in-process Actor, the same domain transaction, and
artifact methods appropriate to their storage. This module has no Studio,
runtime, filesystem, transport or model-provider dependency.
"""

from .contracts import Actor, TaskCreateInput
from .errors import TeamsError


class TeamsToolMethods:
    @staticmethod
    def tool_definitions():
        def schema(properties, required=()):
            return {
                "type": "object",
                "properties": properties,
                "required": list(required),
                "additionalProperties": False,
            }

        text = {"type": "string"}
        return [
            ("team_context", "读取当前团队消息、成员和任务状态。", schema({})),
            (
                "team_message",
                "发布进度，或明确请求某成员执行；wake=false仅留言，不触发执行。",
                schema(
                    {"content": text, "targetMemberId": text, "wake": {"type": "boolean"}},
                    ["content"],
                ),
            ),
            (
                "team_create_task",
                "仅Leader：创建有验收标准的任务；指定负责人即自动派发，不要再额外wake。依赖用本轮已有taskId。",
                TaskCreateInput.model_json_schema(),
            ),
            (
                "team_wait",
                "仅Leader：持久等待一组任务验收/失败，不阻塞；调用后结束当前回复。",
                schema(
                    {"taskIds": {"type": "array", "items": text, "minItems": 1, "maxItems": 64}},
                    ["taskIds"],
                ),
            ),
            (
                "team_task_action",
                "按最新 revision 领取任务；Leader 分配/重试，并仅在leader策略下审核任务结果。",
                schema(
                    {
                        "taskId": text,
                        "action": {"enum": ["assign", "claim", "retry", "accept", "reject"]},
                        "expectedRevision": {"type": "integer", "minimum": 1},
                        "ownerMemberId": text,
                        "reason": {"type": "string", "maxLength": 2000},
                    },
                    ["taskId", "action", "expectedRevision"],
                ),
            ),
            (
                "team_submit_result",
                "提交自己当前任务的可核验结果。成功Run结束及验收后才算成功。",
                schema(
                    {"result": text, "artifactIds": {"type": "array", "items": text}}, ["result"]
                ),
            ),
            (
                "team_finish",
                "仅Leader：所有任务通过验收后提交最终成果，等待群主验收。",
                schema({"result": text}, ["result"]),
            ),
            (
                "team_publish_artifact",
                "登记当前独立工作区的实际交付文件，返回有来源和哈希的产物引用。",
                schema({"path": text, "name": text}, ["path"]),
            ),
            (
                "team_read_artifact",
                "读取本群已显式共享的不可变交付物，复制到自己工作区并返回来源与摘要。",
                schema({"artifactId": text}, ["artifactId"]),
            ),
        ]

    async def invoke(self, principal, operation, arguments, call_id):
        # principal is an in-process/opaque-handle Host value, never JSON Actor.
        actor = principal.get("actor") if isinstance(principal, dict) else None
        if not isinstance(actor, Actor) or actor.kind != "member":
            raise TeamsError("invocation_required", "缺少宿主授予的成员调用上下文", status=403)
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError

        definition = next((item for item in self.tool_definitions() if item[0] == operation), None)
        if definition is None:
            raise TeamsError("tool_unavailable", "当前团队不提供此工具", status=404)
        try:
            Draft202012Validator(definition[2]).validate(arguments)
        except ValidationError as error:
            raise TeamsError(
                "invalid_tool_arguments", "协作工具参数不符合声明的结构", status=422
            ) from error
        if not isinstance(call_id, str) or not call_id or len(call_id) > 512:
            raise TeamsError("call_identity_required", "缺少稳定工具调用标识", status=403)
        with self.domain.store.transaction() as tx:
            self.domain.authorize(tx, actor, actor.group_id)
        key = f"tool:{actor.run_id}:{call_id}"
        if operation == "team_context":
            from .context import execution_context

            return execution_context(self.domain, actor, principal.get("taskId"))
        if operation == "team_message":
            return self.domain.member_message(
                actor,
                content=arguments["content"],
                target_member_id=arguments.get("targetMemberId"),
                wake=arguments.get("wake", False),
                source_delivery_id=principal["deliveryId"],
                key=key,
            )
        if operation == "team_create_task":
            return self.domain.create_task(
                actor,
                actor.group_id,
                actor.team_run_id,
                TaskCreateInput.model_validate(arguments),
                key,
            )
        if operation == "team_wait":
            return self.domain.wait_for_tasks(actor, arguments["taskIds"], key)
        if operation == "team_task_action":
            return self.domain.task_action(
                actor,
                actor.group_id,
                arguments["taskId"],
                arguments["action"],
                arguments["expectedRevision"],
                key,
                owner_member_id=arguments.get("ownerMemberId"),
                reason=arguments.get("reason"),
            )
        if operation == "team_submit_result":
            artifacts = self._artifacts(actor, arguments.get("artifactIds", []))
            return self.domain.result_candidate(
                actor, principal["taskId"], arguments["result"], key, artifacts=artifacts
            )
        if operation == "team_finish":
            return self.domain.finish_candidate(actor, arguments["result"], key)
        if operation == "team_publish_artifact":
            return self.publish_artifact(actor, arguments["path"], arguments.get("name"), key)
        if operation == "team_read_artifact":
            return self.read_artifact(actor, arguments["artifactId"])
        raise TeamsError("tool_unavailable", "当前团队不提供此工具", status=404)
