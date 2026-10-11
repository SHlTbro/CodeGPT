"""The candidate MCP entry shares owner consent and the real task manager."""
import asyncio
import json
from pydantic import PrivateAttr
from fastmcp.tools.base import Tool, ToolResult
from fastmcp.server.dependencies import get_access_token
from mcp.types import ToolAnnotations

from local_agents.access import build_candidate_server
from local_agents.contracts import CONTRACT_PATH


def _schema(value,definitions):
    if isinstance(value,list):return [_schema(v,definitions) for v in value]
    if not isinstance(value,dict):return value
    if '$ref' in value:
        return _schema(definitions[value['$ref'].split('/')[-1]],definitions)
    return {k:_schema(v,definitions) for k,v in value.items()}


class _TaskTool(Tool):
    _service=PrivateAttr()
    _provider=PrivateAttr()

    async def run(self,arguments):
        token=get_access_token()
        owner=await self._provider.resolve_owner(token.token if token else '')
        method=getattr(self._service,self.name)
        # Poll waits/hash reads may block; never stall the authentication loop.
        result=await asyncio.to_thread(method,owner,arguments)
        # Recheck revocation before releasing data from a slow poll/read.
        await self._provider.resolve_owner(token.token)
        return ToolResult(content=[],structured_content=result)


def build_agent_server(provider,service):
    if not provider.task_access:raise ValueError('Task tools need explicit context consent')
    server=build_candidate_server(provider)
    definitions=json.loads(CONTRACT_PATH.read_bytes())['$defs']
    for name in ('project_context','task_poll','task_cancel'):
        tool=_TaskTool(name=name,description={'project_context':'Request scoped project context or reuse a saved report; execution may be unavailable.',
            'task_poll':'Read only the current owner task/report, with bounded paging and no inference.',
            'task_cancel':'Cancel an owned task after native process cleanup.'}[name],
            parameters=_schema(definitions[name+'_request'],definitions),
            annotations=ToolAnnotations(read_only_hint=name!='task_cancel',open_world_hint=False,destructive_hint=False,idempotent_hint=True),
            meta={'securitySchemes':[{'type':'oauth2','scopes':provider.grant_scopes}]})
        tool._service=service;tool._provider=provider
        server.add_tool(tool)
    return server
