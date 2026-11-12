"""Actual official A2A client against the test Gateway HTTPS listener."""

import asyncio
import json
import ssl
import sys
from contextlib import aclosing
from pathlib import Path

import httpx
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.types import CancelTaskRequest, GetTaskRequest, SendMessageRequest, SubscribeToTaskRequest
from google.protobuf.json_format import MessageToDict, ParseDict

from ksadk.a2a.product_key import product_key_http_client


async def main():
    config = json.loads(sys.stdin.readline())
    wire = config["wire"]
    streaming = wire["method"] in {"SendStreamingMessage", "SubscribeToTask"}
    async with product_key_http_client(config["origin"], api_key=config["key"]) as http:
        # Use the fixture CA without weakening verification or replacing the network.
        await http._transport.aclose()
        http._transport = httpx.AsyncHTTPTransport(
            verify=ssl.create_default_context(cafile=config["ca_file"]), trust_env=False)
        if config.get("release_stream"):
            http.headers["x-r87-resume"] = "1"
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(cafile=config["ca_file"]), trust_env=False,
        ) as discovery_http:
            card = await A2ACardResolver(discovery_http, config["origin"]).get_agent_card()
        assert card.capabilities.streaming
        assert "agentengineProductApiKey" in card.security_schemes
        assert any("agentengineProductApiKey" in requirement.schemes
                   for requirement in card.security_requirements)
        assert card.supported_interfaces[0].url == config["origin"] + "/a2a/jsonrpc"
        client = await create_client(
            agent=card, client_config=ClientConfig(httpx_client=http, streaming=streaming))
        try:
            if config.get("expect_terminal_subscription"):
                from a2a.utils.errors import UnsupportedOperationError

                assert wire["method"] == "SubscribeToTask"
                try:
                    async for _ in client.subscribe(
                        ParseDict(wire["params"], SubscribeToTaskRequest())
                    ):
                        raise AssertionError("terminal task emitted a subscription event")
                except UnsupportedOperationError:
                    print(json.dumps({"events": [{"error": "terminal_subscription"}]}), flush=True)
                    return
                raise AssertionError("terminal subscription completed without expected error")
            if streaming:
                stream = (client.subscribe(ParseDict(wire["params"], SubscribeToTaskRequest()))
                          if wire["method"] == "SubscribeToTask" else
                          client.send_message(ParseDict(wire["params"], SendMessageRequest())))
                events = []
                async with aclosing(stream):
                    async for event in stream:
                        events.append({"jsonrpc": "2.0", "id": wire["id"],
                                       "result": MessageToDict(event)})
                        if config.get("output_observed_file"):
                            Path(config["output_observed_file"] + ".progress").write_text(
                                json.dumps(events))
                        if config.get("output_observed_file") and event.HasField("artifact_update"):
                            if any(part.text == "partial-before-crash"
                                   for part in event.artifact_update.artifact.parts):
                                Path(config["output_observed_file"]).write_text(
                                json.dumps(events))
                        if config.get("disconnect_after_task") and event.HasField("task"):
                            break
                assert events
                print(json.dumps({"events": events}), flush=True)
                return
            if wire["method"] == "GetTask":
                task = await client.get_task(ParseDict(wire["params"], GetTaskRequest()))
            elif wire["method"] == "CancelTask":
                task = await client.cancel_task(ParseDict(wire["params"], CancelTaskRequest()))
            elif wire["method"] == "SendMessage":
                responses = [event async for event in client.send_message(
                    ParseDict(wire["params"], SendMessageRequest()))]
                assert len(responses) == 1
                task = responses[0]
                assert task.HasField("task") or task.HasField("message")
            else:
                raise AssertionError("unsupported official client fixture method")
            print(json.dumps({"jsonrpc": "2.0", "id": wire["id"],
                              "result": MessageToDict(task)}), flush=True)
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
