"""Project-scoped ClipOS MCP tools over the existing HTTP API.

Run with a dedicated environment from requirements-mcp.txt. The server cannot
access the ClipOS database or queue directly; edits/renders use the API gates.
"""
import os
from urllib.parse import urlparse
from uuid import UUID

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


def _config() -> tuple[str, str]:
    job_id = os.environ.get('CLIPOS_MCP_JOB_ID', '')
    try:
        UUID(job_id)
    except ValueError as exc:
        raise RuntimeError('Set CLIPOS_MCP_JOB_ID to the project job UUID') from exc
    base = os.environ.get('CLIPOS_API_URL', 'http://127.0.0.1:8000').rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise RuntimeError('CLIPOS_API_URL must be a local HTTP origin')
    return job_id, base


def _api(method: str, path: str, *, params: dict | None = None, body: dict | None = None):
    job_id, base = _config()
    try:
        with httpx.Client(base_url=base, timeout=30.0) as client:
            response = client.request(method, f'/jobs/{job_id}{path}', params=params, json=body)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        detail = response.json().get('detail', 'Request rejected') if response.status_code < 500 else 'Backend request failed'
        raise ToolError(f'ClipOS API {response.status_code}: {str(detail)[:500]}') from exc
    except httpx.RequestError as exc:
        raise ToolError('ClipOS backend is unavailable') from exc


server = MCPServer(name='ClipOS', instructions='Tools operate only on the configured ClipOS job. Render work is asynchronous; poll its durable render ID.')


@server.tool()
def search_moments(query: str, limit: int = 20) -> list[dict]:
    """Search this project's candidate hooks, reasons and transcript text."""
    return _api('GET', '/moments/search', params={'q': query, 'limit': limit})


@server.tool()
def get_job_status() -> dict:
    """Get current project and automatic pipeline state."""
    return _api('GET', '/queue_status')


@server.tool()
def get_clip_draft(clip_id: str) -> dict:
    """Create or read a selected clip's revisioned edit recipe."""
    return _api('GET', f'/clips/{UUID(clip_id)}/edit')


@server.tool()
def save_clip_edit(clip_id: str, expected_revision: int, recipe: dict) -> dict:
    """Save a recipe with revision conflict and source validation enforced by ClipOS."""
    return _api('PUT', f'/clips/{UUID(clip_id)}/edit', body={'expected_revision': expected_revision, 'recipe': recipe})


@server.tool()
def request_clip_render(clip_id: str, revision_id: str, kind: str = 'final') -> dict:
    """Queue a tracked render of an immutable revision and return its render/task IDs."""
    return _api('POST', f'/clips/{UUID(clip_id)}/renders', body={'revision_id': str(UUID(revision_id)), 'kind': kind})


@server.tool()
def get_clip_renders(clip_id: str) -> list[dict]:
    """Get durable render status and available outputs for this clip."""
    return _api('GET', f'/clips/{UUID(clip_id)}/renders')


@server.tool()
def export_resolve_handoff(clip_ids: list[str] | None = None) -> dict:
    """Export selected source ranges, markers and SRT for optional Resolve editing."""
    return _api('POST', '/resolve-export', body={'clip_ids': [str(UUID(item)) for item in clip_ids] if clip_ids else None})


if __name__ == '__main__':
    _config()
    server.run(transport='stdio')
