"""Map internal observations to the provider-neutral event contract."""

EVENT_KINDS = {
    "user": "message.created",
    "run_started": "run.started",
    "session": "session.started",
    "assistant": "message.completed",
    "text_delta": "message.delta",
    "tool_call": "tool.started",
    "tool_result": "tool.completed",
    "subagent": "subagent.status",
    "usage": "usage.observed",
    "quota": "quota.observed",
    "permission_required": "permission.required",
    "permission_denied": "permission.denied",
    "permission_response": "permission.responded",
    "run_finished": "run.finished",
    "recovery": "recovery.observed",
    "gap": "recovery.gap",
}


def public_event(event, engine=None):
    """Return the stable event shape without exposing provider event names."""
    kind = EVENT_KINDS.get(event.kind, "provider.event")
    return {
        "seq": event.seq,
        "turn_id": event.run_id,
        "instance_id": event.session_id,
        "message_id": event.data.get("message_id", event.run_id),
        "engine": engine,
        "kind": kind,
        "at": event.at,
        "data": dict(event.data or {}),
        "final": kind in {"message.completed", "run.finished"},
    }
