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
    "error": "run.error",
    "compaction_started": "context.compacting",
    "compaction": "context.compacted",
    "model_changed": "model.changed",
    "route_selected": "route.selected",
    "retry": "run.retrying",
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
    data = dict(event.data or {})
    if event.kind == 'quota':
        from .quota_windows import project
        data = project(engine, {**data, 'observed_at': event.at, 'source': data.get('source', 'provider_event'), 'supported': True})
    return {
        "seq": event.seq,
        "turn_id": event.run_id,
        "instance_id": event.session_id,
        "message_id": event.data.get("message_id", event.run_id),
        "engine": engine,
        "kind": kind,
        "at": event.at,
        "data": data,
        "final": kind == 'run.finished' or (kind == 'message.completed' and not data.get('incomplete')),
    }
