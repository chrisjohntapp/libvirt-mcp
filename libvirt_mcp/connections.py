import logging
import urllib.parse

import libvirt

from libvirt_mcp.app import mcp
from libvirt_mcp.common import _format_error, _run
from libvirt_mcp.models import ConnectHostInput, HostInput
from libvirt_mcp.state import _connections

logger = logging.getLogger("libvirt_mcp")


def _get_conn(host_alias: str) -> libvirt.virConnect:
    """Return a live connection for the given alias, or raise."""
    conn = _connections.get(host_alias)
    if conn is None:
        raise ValueError(
            f"No connection found for '{host_alias}'. Use libvirt_connect_host first."
        )
    try:
        conn.getVersion()
    except libvirt.libvirtError:
        _connections.pop(host_alias, None)
        raise ValueError(
            f"Connection to '{host_alias}' has dropped. "
            "Use libvirt_connect_host to reconnect."
        )
    return conn


@mcp.tool(name="libvirt_connect_host")
async def libvirt_connect_host(params: ConnectHostInput) -> str:
    """Open an SSH connection to a remote libvirt host and register it under an alias.
    Must be called before using any other libvirt tools for this host.
    Uses qemu+ssh:// transport with key-based auth."""
    try:
        user_part = f"{params.user}@" if params.user else ""
        port_part = f":{params.port}" if params.port else ""
        uri = f"qemu+ssh://{user_part}{params.host}{port_part}/system"

        if params.ssh_key_path:
            key = urllib.parse.quote(params.ssh_key_path, safe="")
            uri += f"?keyfile={key}"

        logger.info("Connecting: %s (alias=%s)", uri, params.alias)
        conn = await _run(libvirt.open, uri)

        if conn is None:
            return f"Error: libvirt.open() returned None for URI '{uri}'"

        old = _connections.pop(params.alias, None)
        if old:
            try:
                old.close()
            except Exception:
                pass

        _connections[params.alias] = conn

        hostname = conn.getHostname()
        v = conn.getLibVersion()
        return (
            f"Connected to '{params.alias}' ({hostname})\n"
            f"  URI: {uri}\n"
            f"  libvirt version: {v // 1_000_000}.{v % 1_000_000 // 1_000}.{v % 1_000}\n"
            f"  Use alias='{params.alias}' in other tools."
        )
    except Exception as e:
        return _format_error(e, f"connecting to {params.host}")


@mcp.tool(name="libvirt_disconnect_host")
async def libvirt_disconnect_host(params: HostInput) -> str:
    """Close the connection to a previously registered libvirt host."""
    conn = _connections.pop(params.alias, None)
    if conn is None:
        return f"No active connection found for alias '{params.alias}'."
    try:
        conn.close()
        return f"Disconnected from '{params.alias}'."
    except Exception as e:
        return _format_error(e, "disconnecting")


@mcp.tool(name="libvirt_list_hosts")
async def libvirt_list_hosts() -> str:
    """List all currently registered libvirt host connections and their status."""
    if not _connections:
        return "No hosts connected. Use libvirt_connect_host to add one."

    lines = [
        "# Connected LibVirt Hosts\n",
        "| Alias | Hostname | Status |",
        "|-------|----------|--------|",
    ]
    for alias, conn in list(_connections.items()):
        try:
            hostname = conn.getHostname()
            lines.append(f"| {alias} | {hostname} | live |")
        except Exception:
            lines.append(f"| {alias} | (unknown) | dropped |")

    return "\n".join(lines)
