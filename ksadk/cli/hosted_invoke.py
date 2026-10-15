"""Invoke platform hosted Agents through their admitted Action surface."""

from __future__ import annotations

import hashlib
import time

import click
import requests

from ksadk.conversations.message_projection import project_session_messages


def try_invoke_hosted_once(*, endpoint, api_key, session_id, message, model, insecure):
    """Return False for ordinary OpenAI servers; never bypass hosted admission."""
    with requests.Session() as transport:
        transport.verify = not insecure
        if api_key:
            transport.headers["Authorization"] = "Bearer " + api_key

        def action(name, body, *, missing_ok=False, request_timeout=45):
            response = transport.post(
                endpoint.rstrip("/") + "/agentengine/api/v1/" + name,
                json=body, timeout=request_timeout, allow_redirects=False,
            )
            if response.is_redirect:
                raise click.ClickException("Hosted Action redirected; request was not replayed")
            if missing_ok and response.status_code == 404:
                return None
            response.raise_for_status()
            envelope = response.json()
            if missing_ok and envelope.get("Code") == 404:
                return None
            if envelope.get("Code") not in (None, 0):
                raise click.ClickException(str(envelope.get("Message") or "Hosted Action failed"))
            data = envelope.get("Data", envelope)
            if not isinstance(data, dict):
                raise click.ClickException("Hosted Action returned no result")
            return data

        try:
            bootstrap = action("GetAgentUiBootstrap", {}, missing_ok=True)
        except requests.HTTPError as exc:
            if exc.response.status_code in (404, 405):
                return False
            raise click.ClickException("Hosted bootstrap authentication or transport failed") from exc
        if bootstrap is None:
            return False
        agent_id = (bootstrap.get("Agent") or {}).get("AgentId")
        if (not agent_id or not isinstance(bootstrap.get("HostedRuntime"), dict)
                or (bootstrap.get("Capabilities") or {}).get("KernelSessionEvents") is not True):
            return False

        session_body = {"AgentId": agent_id, "SessionId": session_id}
        session = action("GetSession", session_body, missing_ok=True)
        if session is None:
            session = action("CreateSession", session_body)
        session = session.get("Session", session)
        session_id = session.get("SessionId") or session.get("Id")
        if not session_id:
            raise click.ClickException("CreateSession returned no session identity")
        body = {
            "AgentId": agent_id, "SessionId": session_id,
            "Messages": [{"role": "user", "content": message}],
            "ApiFormat": "chat_completions", "Stream": False, "Background": True,
        }
        if model:
            body["Model"] = model
        receipt = action("RunAgent", body)
        run_id = receipt.get("RunId") or receipt.get("InvocationId")
        command_id, message_id = receipt.get("CommandId"), receipt.get("MessageId")
        if not run_id and not (command_id and message_id):
            raise click.ClickException("Hosted run returned no observable command identity")
        click.echo(f"Session: {session_id}\nCommand: {command_id or run_id}")
        cursor = 0
        cursor_source = None
        claimed_message = None
        events = {}
        submitted = set()
        resolved = set()
        pending = {}
        terminal = None
        deadline = time.monotonic() + 900
        read_interrupted = False
        while (remaining := deadline - time.monotonic()) > 0:
            query = {
                "AgentId": agent_id, "SessionId": session_id,
                "AfterSeqId": cursor, "Limit": 200, "EventOrder": "session_seq",
            }
            if cursor_source:
                query["CursorSource"] = cursor_source
            try:
                page = action("ListSessionEvents", query, request_timeout=min(45, remaining))
            except requests.exceptions.SSLError:
                raise
            except (requests.ConnectionError, requests.Timeout):
                # This is an idempotent observation of an already admitted run.
                # Preserve its cursor and original deadline; never replay a run
                # or an interaction whose receipt may have been lost.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if not read_interrupted:
                    click.echo("Event read interrupted; retrying within the original wait deadline", err=True)
                    read_interrupted = True
                time.sleep(min(1, remaining))
                continue
            source = page.get("CursorSource")
            if (page.get("EventOrder") != "session_seq" or source not in ("runtime", "control")
                    or type(page.get("HasMore")) is not bool):
                raise click.ClickException(
                    "Hosted Runtime does not support forward session event reads; upgrade Runtime. "
                    f"Run continues. Session: {session_id}, Command: {command_id}, Run: {run_id}"
                )
            if cursor_source and source != cursor_source:
                raise click.ClickException("Hosted event cursor source changed; inspect the session above")
            cursor_source = source
            rows = page.get("Events")
            next_cursor = page.get("NextCursor")
            if (not isinstance(rows, list) or type(next_cursor) is not int
                    or any(not isinstance(e, dict) or type(e.get("SeqId")) is not int for e in rows)):
                raise click.ClickException("Hosted Runtime returned an invalid forward event page")
            rows = sorted(rows, key=lambda e: e["SeqId"])
            seqs = [e["SeqId"] for e in rows]
            if (any(seq <= cursor for seq in seqs) or len(set(seqs)) != len(seqs)
                    or next_cursor != (seqs[-1] if seqs else cursor)
                    or (page["HasMore"] and not rows)):
                raise click.ClickException("Hosted Runtime returned a non-progressing forward event page")
            cursor = next_cursor
            for event in rows:
                content = event.get("Content") or {}
                fact = content.get("runtime_event") or (content.get("session_event") or {}).get("payload") or {}
                kind = event.get("EventType")
                if not run_id:
                    if kind == "control.command_rejected" and fact.get("command_id") == command_id:
                        raise click.ClickException("Admitted command was rejected by the worker")
                    if kind == "control.message_claimed":
                        claimed_message = fact.get("message_id")
                    # Session worker serializes claim -> pending transition.
                    # An accepted enqueue has no RunId until its own message is claimed.
                    if (kind == "control.run_transition" and fact.get("state") == "pending"
                            and claimed_message == message_id and fact.get("run_id")):
                        run_id = fact["run_id"]
                        click.echo(f"Run: {run_id}")
                    else:
                        continue
                if fact.get("run_id", event.get("InvocationId")) != run_id:
                    continue
                if not fact.get("parent_scope_id"):
                    events[event.get("EventId") or str(event["SeqId"])] = event
                if kind == "interaction.requested":
                    pending[(fact.get("interaction_id"), fact.get("revision"))] = fact
                if kind in ("interaction.resolved", "interaction.expired"):
                    resolved.add(fact.get("interaction_id"))
                if kind in ("run.completed", "run.failed", "run.canceled") and not fact.get("parent_scope_id"):
                    terminal = kind
            # Decisions require the whole available forward window: resolution
            # or the root terminal may be on a later page than the request.
            if page["HasMore"]:
                continue
            for key, fact in pending.items():
                interaction_id = fact.get("interaction_id")
                revision = fact.get("revision")
                if terminal or key in submitted or interaction_id in resolved:
                    continue
                request = fact.get("request") or {}
                presentation = request.get("presentation") or {}
                click.echo(presentation.get("title") or "Interaction required")
                click.echo(presentation.get("description") or "")
                if request.get("kind") != "approval" or not interaction_id or not revision:
                    raise click.ClickException("Run requires input; continue in Dashboard using the session above")
                try:
                    approved = click.confirm("批准并继续？", default=False)
                except click.Abort:
                    raise click.ClickException("Run is waiting for approval; continue in Dashboard using the session above") from None
                if time.monotonic() >= deadline:
                    break
                decision = "approve" if approved else "reject"
                token = hashlib.sha256(f"{run_id}:{interaction_id}:{revision}:{decision}".encode()).hexdigest()
                action("SubmitInteraction", {
                    "AgentId": agent_id, "SessionId": session_id, "RunId": run_id,
                    "InteractionId": interaction_id, "ExpectedRevision": revision,
                    "InteractionAction": decision, "Response": {"decision": decision},
                    "IdempotencyKey": "cli-" + token,
                })
                submitted.add(key)
            if terminal:
                if terminal != "run.completed":
                    raise click.ClickException(f"Hosted run ended with {terminal}; Session: {session_id}, Run: {run_id}")
                projected = project_session_messages(list(events.values()))
                answers = [m for m in projected if m.get("Role") == "assistant" and (m.get("Content") or {}).get("text")]
                if not answers:
                    raise click.ClickException("Completed run has no text result; inspect the session above")
                click.echo((answers[-1]["Content"] or {})["text"])
                return True
            time.sleep(1)
        raise click.ClickException(f"Waiting timed out; run continues. Session: {session_id}, Command: {command_id}, Run: {run_id}")
