from scripts.validate_hosted_long_task_e2e import (
    HostedE2EError,
    validate_cancel_then_resume,
    _wait_for_checkpoint,
)


class FlakyCheckpointClient:
    agent_id = "ar-test"

    def __init__(self):
        self.calls = 0

    def action(self, name, payload):
        self.calls += 1
        if name == "ListSessionCheckpoints" and self.calls == 1:
            raise HostedE2EError(
                "ListSessionCheckpoints returned Code=404: {'Code': 404}"
            )
        if name == "ListSessionCheckpoints":
            return {
                "Data": {
                    "Checkpoints": [
                        {
                            "RunId": payload.get("RunId") or "run-1",
                            "CheckpointId": "checkpoint-1",
                        }
                    ]
                }
            }
        raise AssertionError(f"unexpected action: {name}")


def test_wait_for_checkpoint_retries_initial_not_found():
    checkpoint = _wait_for_checkpoint(
        FlakyCheckpointClient(),
        session_id="session-1",
        run_id="run-1",
        attempts=2,
        interval=0,
    )

    assert checkpoint["CheckpointId"] == "checkpoint-1"


class BackgroundRunClient:
    agent_id = "ar-test"
    user_id = "user"

    def __init__(self):
        self.cancel_invocation_id = ""
        self.resume_payload = {}
        self.event_calls = 0

    def action(self, name, payload):
        if name == "GetAgentUiBootstrap":
            return {
                "Data": {
                    "Capabilities": {
                        "RunLifecycle": {
                            "Checkpoints": True,
                            "CheckpointResume": True,
                        }
                    }
                }
            }
        if name == "ListSessionCheckpoints":
            return {
                "Data": {
                    "Checkpoints": [
                        {
                            "RunId": "run-background",
                            "CheckpointId": "checkpoint-1",
                        }
                    ]
                }
            }
        if name == "CancelRun":
            self.cancel_invocation_id = payload["InvocationId"]
            return {"Data": {"Cancelled": True}}
        if name == "ListSessionEvents":
            self.event_calls += 1
            checkpoint_events = [
                {"EventType": "run_checkpoint", "InvocationId": "run-resume"}
            ]
            if self.event_calls > 2:
                checkpoint_events.append(
                    {"EventType": "run_checkpoint", "InvocationId": "run-resume"}
                )
            return {
                "Data": {
                    "Events": [
                        {
                            "EventType": "run_status",
                            "InvocationId": "longtask_run-background",
                            "Content": {"status": "cancelled"},
                        },
                        {"EventType": "run_resume", "InvocationId": "run-resume"},
                        *checkpoint_events,
                        {
                            "EventType": "run_status",
                            "InvocationId": "run-resume",
                            "Content": {"status": "completed"},
                        },
                    ]
                }
            }
        raise AssertionError(f"unexpected action: {name}")

    def stream_action(self, name, payload, *, max_seconds):
        if name == "RunAgent":
            return "data: started"
        if name == "ResumeRun":
            self.resume_payload = payload
            return "data: resumed"
        raise AssertionError(f"unexpected stream action: {name}")


def test_cancel_then_resume_cancels_background_run_id_from_checkpoint():
    client = BackgroundRunClient()

    result = validate_cancel_then_resume(
        client,
        session_id="session-1",
        prompt="run until checkpoint",
        wait_attempts=1,
        wait_interval=0,
        stream_timeout=1,
    )

    assert client.cancel_invocation_id == "longtask_run-background"
    assert client.resume_payload["RunId"] == "run-background"
    assert result["run_agent_invocation_id"].startswith("run_")
    assert result["cancel_invocation_id"] == "longtask_run-background"
