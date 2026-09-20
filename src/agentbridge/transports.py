"""Construct argv without shell evaluation; provider permissions remain explicit."""
import json
import sys
from .errors import UnsupportedError


def command(account, session, options):
    if options.context_window is not None:
        raise UnsupportedError('Context window selection is not supported by this adapter.')
    native=session.get('native_id')
    model=options.model or session.get('model')
    if account.engine=='codex':
        if options.permission_mode != 'dontAsk':
            raise UnsupportedError('Codex exec does not support interactive permission policies.')
        if options.max_turns is not None or options.max_budget_usd is not None or options.allowed_tools:
            raise UnsupportedError('Codex exec does not support AgentBridge turn, tool-list or dollar caps.')
        cmd=list(account.command or ('codex',))+['exec']
        if native:cmd+=['resume',native]
        cmd+=['--json','--skip-git-repo-check','-c','approval_policy="never"','-c',f'sandbox_mode={json.dumps(options.sandbox)}']
        if model:cmd+=['--model',model]
        if options.effort:cmd+=['-c',f'model_reasoning_effort={json.dumps(options.effort)}']
        return cmd+['-']
    if account.engine=='claude':
        if options.sandbox != 'read-only':
            raise UnsupportedError('Claude uses permission_mode and allowed_tools, not Codex sandbox modes.')
        cmd=list(account.command or ('claude',))+['-p','--output-format','stream-json','--verbose','--include-partial-messages',
            '--permission-mode',options.permission_mode]
        if native:cmd+=['--resume',native]
        if model:cmd+=['--model',model]
        if options.effort:cmd+=['--effort',options.effort]
        if options.allowed_tools:cmd+=['--allowedTools',','.join(options.allowed_tools)]
        if options.max_turns is not None:cmd+=['--max-turns',str(options.max_turns)]
        if options.max_budget_usd is not None:cmd+=['--max-budget-usd',str(options.max_budget_usd)]
        return cmd
    if options.max_turns is not None or options.max_budget_usd is not None or options.effort:
        raise UnsupportedError('Cursor adapter does not map turn/dollar caps or effort yet.')
    if options.permission_mode != 'dontAsk':
        raise UnsupportedError('Cursor interactive permission responses are not connected.')
    if options.sandbox == 'workspace-write':
        raise UnsupportedError('Cursor does not provide the same workspace-write policy as Codex.')
    if options.sandbox == 'read-only' and options.allowed_tools:
        raise UnsupportedError('Cursor read-only mode disables tools; a tool list would conflict.')
    return list(account.command or (sys.executable,'-m','agentbridge.cursor_worker'))
