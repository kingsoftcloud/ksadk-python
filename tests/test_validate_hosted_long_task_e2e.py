from scripts.validate_hosted_long_task_e2e import HostedE2EError, _wait_for_checkpoint


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
