from ksadk.resource_runtime.ipc import ResourceRequest
from ksadk.resource_runtime.policy_authorization import FullAccessResourceWriteAuthorizer
from tests.resource_runtime.test_broker import scope as resource_scope


async def test_full_access_authorization_is_exact_and_stable() -> None:
    bound = resource_scope().model_copy(
        update={"allowed_operations": ("load_memory", "save_memory")}
    )
    request = ResourceRequest.model_validate(
        {
            "v": 1,
            "requestId": "call-a",
            "handle": "x" * 32,
            "deadline": 1,
            "operation": "save_memory",
            "arguments": {"content": "remember me"},
        }
    )
    authorizer = FullAccessResourceWriteAuthorizer(activation_key="session-a")

    first = await authorizer.authorize(request, bound)
    assert first and first.startswith("studio-full-access:")
    assert await authorizer.authorize(request, bound) == first
    assert await authorizer.authorize(
        request.model_copy(update={"arguments": {"content": "different"}}), bound
    ) != first
    assert await authorizer.authorize(
        request.model_copy(update={"operation": "load_memory"}), bound
    ) is None


async def test_full_access_authorization_still_obeys_scope() -> None:
    bound = resource_scope().model_copy(update={"allowed_operations": ("load_memory",)})
    request = ResourceRequest.model_validate(
        {
            "v": 1,
            "requestId": "call-a",
            "handle": "x" * 32,
            "deadline": 1,
            "operation": "save_memory",
            "arguments": {"content": "remember me"},
        }
    )

    assert await FullAccessResourceWriteAuthorizer(
        activation_key="session-a"
    ).authorize(request, bound) is None
