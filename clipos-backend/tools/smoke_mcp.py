"""Exercise a representative edit/export request through a real MCP stdio client.

Requires CLIPOS_MCP_JOB_ID, CLIPOS_API_URL, and a selected clip UUID argument.
This checks tool routing and durable task IDs; it is not a language-model test.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def main(clip_id: str):
    python = Path(sys.executable)
    server = Path(__file__).resolve().parents[1]/'mcp_server.py'
    async with Client(StdioServerParameters(command=str(python), args=[str(server)], env=os.environ.copy())) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {'search_moments', 'get_clip_draft', 'save_clip_edit', 'request_clip_render',
                'get_clip_renders', 'export_resolve_handoff', 'get_job_status'} <= names

        async def call(name, arguments):
            response = await client.call_tool(name, arguments)
            if response.is_error:
                raise RuntimeError(response.content[0].text)
            value = response.structured_content or json.loads(response.content[0].text)
            return value['result'] if isinstance(value, dict) and 'result' in value else value

        found = await call('search_moments', {'query': 'comeback'})
        assert any(item['clip_id'] == clip_id for item in found)
        await call('get_job_status', {})
        draft = await call('get_clip_draft', {'clip_id': clip_id})
        invalid = json.loads(json.dumps(draft['recipe']))
        invalid['source']['fingerprint'] = '0'*64
        rejected = await client.call_tool('save_clip_edit', {'clip_id': clip_id,
            'expected_revision': draft['revision'], 'recipe': invalid})
        assert rejected.is_error and 'ClipOS API 422' in rejected.content[0].text
        recipe = draft['recipe']
        recipe['captions']['words'][0]['word'] = 'Victory'
        saved = await call('save_clip_edit', {'clip_id': clip_id,
            'expected_revision': draft['revision'], 'recipe': recipe})
        render = await call('request_clip_render', {'clip_id': clip_id,
            'revision_id': saved['revision_id'], 'kind': 'final'})
        assert render['render_id'] and render['rq_task_id'] and render['revision_id'] == saved['revision_id']
        renders = await call('get_clip_renders', {'clip_id': clip_id})
        assert any(item['render_id'] == render['render_id'] for item in renders)
        handoff = await call('export_resolve_handoff', {'clip_ids': [clip_id]})
        assert handoff['clip_count'] == 1 and handoff['timeline_url']
        print(json.dumps({'tool_count': len(names), 'revision_id': saved['revision_id'],
                          'render_id': render['render_id'], 'rq_task_id': render['rq_task_id'],
                          'handoff_id': handoff['handoff_id']}))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
