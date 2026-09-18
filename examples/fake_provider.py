#!/usr/bin/env python3
"""A deterministic provider fixture used for local integration examples."""
import json,sys
print(json.dumps({'type':'thread.started','thread_id':'fixture-session'}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'AgentBridge is running.'}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':4,'output_tokens':3}}),flush=True)
