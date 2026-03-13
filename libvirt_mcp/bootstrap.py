"""Imports tool modules so decorators register all MCP tools."""

from libvirt_mcp import connections as _connections  # noqa: F401
from libvirt_mcp import create_vm as _create_vm  # noqa: F401
from libvirt_mcp import delete_vm as _delete_vm  # noqa: F401
from libvirt_mcp import domains as _domains  # noqa: F401
from libvirt_mcp import migration as _migration  # noqa: F401
