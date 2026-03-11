#!/usr/bin/env python3
"""LibVirt MCP Server -- manages VMs on remote libvirt hosts via SSH."""

import json
import asyncio
import logging
import urllib.parse
from enum import Enum

import libvirt
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("libvirt_mcp")

_connections: dict[str, libvirt.virConnect] = {}


def _get_conn(host_alias: str) -> libvirt.virConnect:
    """Return a live connection for the given alias, or raise."""
    conn = _connections.get(host_alias)
    if conn is None:
        raise ValueError(
            f"No connection found for '{host_alias}'. "
            "Use libvirt_connect_host first."
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


_STATE_MAP = {
    libvirt.VIR_DOMAIN_NOSTATE: "no state",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutting down",
    libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended (PM)",
}


def _domain_state_str(state_code: int) -> str:
    return _STATE_MAP.get(state_code, f"unknown ({state_code})")


def _domain_summary(dom: libvirt.virDomain) -> dict:
    """Return a concise dict summary of a domain."""
    info = dom.info()  # [state, maxMem, memory, nrVirtCpu, cpuTime]
    try:
        autostart = bool(dom.autostart())
    except libvirt.libvirtError:
        autostart = None
    return {
        "name": dom.name(),
        "uuid": dom.UUIDString(),
        "state": _domain_state_str(info[0]),
        "max_memory_mb": info[1] // 1024,
        "current_memory_mb": info[2] // 1024,
        "vcpus": info[3],
        "persistent": bool(dom.isPersistent()),
        "autostart": autostart,
    }


def _lookup_domain(conn: libvirt.virConnect, domain: str) -> libvirt.virDomain:
    """Resolve domain by name or UUID."""
    try:
        return conn.lookupByName(domain)
    except libvirt.libvirtError:
        pass
    try:
        return conn.lookupByUUIDString(domain)
    except libvirt.libvirtError:
        raise ValueError(
            f"Domain '{domain}' not found by name or UUID on this host."
        )


def _format_error(e: Exception, context: str = "") -> str:
    prefix = f"Error ({context}): " if context else "Error: "
    if isinstance(e, (libvirt.libvirtError, ValueError)):
        return f"{prefix}{e}"
    return f"{prefix}{type(e).__name__}: {e}"


mcp = FastMCP("libvirt_mcp")


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


_MODEL_CONFIG = {"str_strip_whitespace": True, "extra": "forbid"}

_ALIAS_FIELD = Field(..., description="Host alias", min_length=1, max_length=64)
_DOMAIN_FIELD = Field(..., description="Domain name or UUID", min_length=1, max_length=256)


class ConnectHostInput(BaseModel):
    model_config = _MODEL_CONFIG

    host: str = Field(
        ...,
        description="Hostname or IP of the libvirt host",
        min_length=1,
        max_length=253,
    )
    alias: str = Field(
        ...,
        description="Short alias for subsequent tool calls (e.g. 'prod')",
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    user: str | None = Field(default=None, description="SSH username", max_length=64)
    port: int | None = Field(default=None, description="SSH port", ge=1, le=65535)
    ssh_key_path: str | None = Field(
        default=None,
        description="Path to SSH private key file",
        max_length=512,
    )


class HostInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD


class DomainInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    domain: str = _DOMAIN_FIELD


class ListDomainsInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    state_filter: str | None = Field(
        default=None,
        description="Filter by state: 'running', 'shutoff', 'paused', 'all' (default: 'all')",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class DefineVMInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    xml: str = Field(..., description="Full libvirt domain XML definition", min_length=10)


class DomainInfoInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    domain: str = _DOMAIN_FIELD
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


async def _run(func, *args):
    """Run a blocking function in the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


@mcp.tool(
    name="libvirt_connect_host",
    annotations={
        "title": "Connect to LibVirt Host",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
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


@mcp.tool(
    name="libvirt_disconnect_host",
    annotations={
        "title": "Disconnect from LibVirt Host",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
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


@mcp.tool(
    name="libvirt_list_hosts",
    annotations={
        "title": "List Connected LibVirt Hosts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def libvirt_list_hosts() -> str:
    """List all currently registered libvirt host connections and their status."""
    if not _connections:
        return "No hosts connected. Use libvirt_connect_host to add one."

    lines = ["# Connected LibVirt Hosts\n", "| Alias | Hostname | Status |", "|-------|----------|--------|"]
    for alias, conn in list(_connections.items()):
        try:
            hostname = conn.getHostname()
            lines.append(f"| {alias} | {hostname} | live |")
        except Exception:
            lines.append(f"| {alias} | (unknown) | dropped |")

    return "\n".join(lines)


@mcp.tool(
    name="libvirt_list_domains",
    annotations={
        "title": "List Domains on Host",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def libvirt_list_domains(params: ListDomainsInput) -> str:
    """List all domains (VMs) on a connected libvirt host, optionally filtered by state."""
    try:
        conn = _get_conn(params.alias)
        all_domains = await _run(conn.listAllDomains, 0)

        state_filter = (params.state_filter or "all").lower()
        valid_filters = {"all", "running", "shutoff", "paused", "blocked", "crashed", "suspended (pm)", "shutting down", "no state"}
        if state_filter not in valid_filters:
            return f"Invalid state_filter '{state_filter}'. Valid values: {', '.join(sorted(valid_filters))}."

        summaries = [
            s for dom in all_domains
            if (s := _domain_summary(dom)) and (state_filter == "all" or s["state"] == state_filter)
        ]
        summaries.sort(key=lambda x: x["name"])

        if not summaries:
            suffix = f" with state='{state_filter}'" if state_filter != "all" else ""
            return f"No domains found on '{params.alias}'{suffix}."

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({"alias": params.alias, "count": len(summaries), "domains": summaries}, indent=2)

        lines = [f"# Domains on '{params.alias}' ({len(summaries)} found)\n"]
        lines.append("| Name | State | vCPUs | Memory (MB) | Persistent |")
        lines.append("|------|-------|-------|-------------|------------|")
        for s in summaries:
            lines.append(
                f"| {s['name']} | {s['state']} | {s['vcpus']} "
                f"| {s['current_memory_mb']} / {s['max_memory_mb']} "
                f"| {'yes' if s['persistent'] else 'no'} |"
            )
        return "\n".join(lines)

    except Exception as e:
        return _format_error(e, "listing domains")


@mcp.tool(
    name="libvirt_get_domain_info",
    annotations={
        "title": "Get Domain Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def libvirt_get_domain_info(params: DomainInfoInput) -> str:
    """Get detailed information about a specific domain."""
    try:
        conn = _get_conn(params.alias)
        dom = await _run(lambda: _lookup_domain(conn, params.domain))
        s = _domain_summary(dom)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(s, indent=2)

        if s["autostart"] is None:
            autostart_str = "n/a (transient)"
        elif s["autostart"]:
            autostart_str = "yes"
        else:
            autostart_str = "no"

        return (
            f"# Domain: {s['name']}\n\n"
            f"- **UUID**: {s['uuid']}\n"
            f"- **State**: {s['state']}\n"
            f"- **vCPUs**: {s['vcpus']}\n"
            f"- **Memory**: {s['current_memory_mb']} MB (max: {s['max_memory_mb']} MB)\n"
            f"- **Persistent**: {'yes' if s['persistent'] else 'no'}\n"
            f"- **Autostart**: {autostart_str}\n"
        )
    except Exception as e:
        return _format_error(e, f"get info for '{params.domain}'")


@mcp.tool(
    name="libvirt_get_domain_xml",
    annotations={
        "title": "Get Domain XML",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def libvirt_get_domain_xml(params: DomainInput) -> str:
    """Retrieve the full XML definition of a domain."""
    try:
        conn = _get_conn(params.alias)
        dom = await _run(lambda: _lookup_domain(conn, params.domain))
        return await _run(dom.XMLDesc, 0)
    except Exception as e:
        return _format_error(e, f"get XML for '{params.domain}'")


async def _domain_action(params: DomainInput, action: str, message: str) -> str:
    """Run a simple domain lifecycle action (start, shutdown, destroy, etc.)."""
    try:
        conn = _get_conn(params.alias)
        dom = await _run(lambda: _lookup_domain(conn, params.domain))
        method = getattr(dom, action)
        if action == "reboot":
            await _run(method, 0)
        else:
            await _run(method)
        return f"{message} '{params.domain}' on '{params.alias}'."
    except Exception as e:
        return _format_error(e, f"{action} '{params.domain}'")


@mcp.tool(
    name="libvirt_start_domain",
    annotations={
        "title": "Start Domain",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def libvirt_start_domain(params: DomainInput) -> str:
    """Start (boot) a shutoff domain."""
    return await _domain_action(params, "create", "Domain started")


@mcp.tool(
    name="libvirt_shutdown_domain",
    annotations={
        "title": "Gracefully Shut Down Domain",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def libvirt_shutdown_domain(params: DomainInput) -> str:
    """Send an ACPI shutdown signal to a running domain (graceful shutdown).
    Use libvirt_destroy_domain to force-stop immediately if needed."""
    return await _domain_action(params, "shutdown", "Shutdown signal sent to")


@mcp.tool(
    name="libvirt_destroy_domain",
    annotations={
        "title": "Force Stop Domain",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def libvirt_destroy_domain(params: DomainInput) -> str:
    """Forcefully stop a running domain immediately. WARNING: not a graceful shutdown."""
    return await _domain_action(params, "destroy", "Domain force-stopped")


@mcp.tool(
    name="libvirt_reboot_domain",
    annotations={
        "title": "Reboot Domain",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def libvirt_reboot_domain(params: DomainInput) -> str:
    """Send a graceful reboot signal to a running domain."""
    return await _domain_action(params, "reboot", "Reboot signal sent to")


@mcp.tool(
    name="libvirt_suspend_domain",
    annotations={
        "title": "Suspend Domain",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def libvirt_suspend_domain(params: DomainInput) -> str:
    """Suspend (pause) a running domain. Use libvirt_resume_domain to unpause."""
    return await _domain_action(params, "suspend", "Domain suspended")


@mcp.tool(
    name="libvirt_resume_domain",
    annotations={
        "title": "Resume Domain",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def libvirt_resume_domain(params: DomainInput) -> str:
    """Resume a suspended (paused) domain."""
    return await _domain_action(params, "resume", "Domain resumed")


@mcp.tool(
    name="libvirt_define_domain",
    annotations={
        "title": "Define Domain from XML",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def libvirt_define_domain(params: DefineVMInput) -> str:
    """Define (register) a new persistent domain from XML. Does NOT start it."""
    try:
        conn = _get_conn(params.alias)
        dom = await _run(lambda: conn.defineXML(params.xml))
        return (
            f"Domain '{dom.name()}' defined on '{params.alias}'.\n"
            f"  UUID: {dom.UUIDString()}\n"
            f"  Use libvirt_start_domain to boot it."
        )
    except Exception as e:
        return _format_error(e, "defining domain")


@mcp.tool(
    name="libvirt_undefine_domain",
    annotations={
        "title": "Undefine (Delete) Domain",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def libvirt_undefine_domain(params: DomainInput) -> str:
    """Undefine (permanently remove) a domain. Domain must be shutoff first.
    Disk images are NOT deleted."""
    try:
        conn = _get_conn(params.alias)
        dom = await _run(lambda: _lookup_domain(conn, params.domain))
        name = dom.name()
        await _run(dom.undefine)
        return (
            f"Domain '{name}' undefined from '{params.alias}'.\n"
            "  Note: Disk images were NOT deleted. Remove them manually if no longer needed."
        )
    except Exception as e:
        return _format_error(e, f"undefining '{params.domain}'")


if __name__ == "__main__":
    mcp.run()
