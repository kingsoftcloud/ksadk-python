"""Pure projection of a Run's current host-bound task contract."""

from .completion import TaskContract
from .ports import RunConfiguration, digest, validate_task_tool_scope
from .state import RunSnapshot


def effective_run_configuration(
    base_configuration: RunConfiguration, snapshot: RunSnapshot,
) -> RunConfiguration:
    if snapshot.task_contract is None:
        return base_configuration
    contract = TaskContract.model_validate(snapshot.task_contract)
    if digest(contract) != snapshot.contract_digest:
        raise ValueError("active contract identity mismatch")
    validate_task_tool_scope(contract, base_configuration.tools)
    configuration = {**base_configuration.model_dump(), "contract": contract}
    if contract.final_submission_schema is not None:
        from .output_contract import submission_output_format

        configuration.update(
            final_output_format=submission_output_format(
                contract.final_submission_schema, contract.final_submission_payload_key
            ),
            final_submission_schema=contract.final_submission_schema,
            final_submission_payload_key=contract.final_submission_payload_key,
        )
    return RunConfiguration.model_validate(configuration)
