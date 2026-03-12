#!/usr/bin/env python3
"""LibVirt MCP Server -- manages VMs on remote libvirt hosts via SSH."""

import copy
import getpass
import json
import asyncio
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

import libvirt
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("libvirt_mcp")

_connections: dict[str, libvirt.virConnect] = {}
_migration_jobs: dict[str, dict] = {}
_migration_jobs_lock = asyncio.Lock()


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
        raise ValueError(f"Domain '{domain}' not found by name or UUID on this host.")


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
_DOMAIN_FIELD = Field(
    ..., description="Domain name or UUID", min_length=1, max_length=256
)


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
    xml: str = Field(
        ..., description="Full libvirt domain XML definition", min_length=10
    )


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
        valid_filters = {
            "all",
            "running",
            "shutoff",
            "paused",
            "blocked",
            "crashed",
            "suspended (pm)",
            "shutting down",
            "no state",
        }
        if state_filter not in valid_filters:
            return f"Invalid state_filter '{state_filter}'. Valid values: {', '.join(sorted(valid_filters))}."

        summaries = [
            s
            for dom in all_domains
            if (s := _domain_summary(dom))
            and (state_filter == "all" or s["state"] == state_filter)
        ]
        summaries.sort(key=lambda x: x["name"])

        if not summaries:
            suffix = f" with state='{state_filter}'" if state_filter != "all" else ""
            return f"No domains found on '{params.alias}'{suffix}."

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(
                {"alias": params.alias, "count": len(summaries), "domains": summaries},
                indent=2,
            )

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


# --- Create VM feature ---


def _load_template(name: str | None) -> dict:
    """Load a VM template from the templates directory."""
    if name is None:
        name = "default"
    path = TEMPLATES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Template '{name}' not found at {path}")
    return json.loads(path.read_text())


def _apply_overrides(
    template: dict,
    *,
    vcpus: int | None = None,
    memory_mb: int | None = None,
    disk_size_gb: int | None = None,
    network_bridge: str | None = None,
) -> dict:
    """Apply user overrides to a template, returning a new dict."""
    result = copy.deepcopy(template)
    if vcpus is not None:
        result["vcpus"] = vcpus
    if memory_mb is not None:
        result["memory_mb"] = memory_mb
    if disk_size_gb is not None:
        result["disk"]["size_gb"] = disk_size_gb
    if network_bridge is not None:
        result["network_bridge"] = network_bridge
    return result


def _build_domain_xml(spec: dict) -> str:
    """Build libvirt domain XML from a resolved spec dict."""
    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = spec["name"]
    ET.SubElement(domain, "memory", unit="KiB").text = str(spec["memory_mb"] * 1024)
    ET.SubElement(domain, "vcpu").text = str(spec["vcpus"])

    os_elem = ET.SubElement(domain, "os")
    os_type = ET.SubElement(os_elem, "type", arch=spec["os"]["arch"])
    os_type.text = spec["os"]["type"]
    ET.SubElement(os_elem, "boot", dev=spec["os"]["boot_dev"])

    devices = ET.SubElement(domain, "devices")

    # Main disk
    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=spec["disk_path"])
    ET.SubElement(disk, "target", dev="vda", bus=spec["disk_bus"])

    # CDROM if boot_iso specified
    if spec.get("boot_iso"):
        cdrom = ET.SubElement(devices, "disk", type="file", device="cdrom")
        ET.SubElement(cdrom, "driver", name="qemu", type="raw")
        ET.SubElement(cdrom, "source", file=spec["boot_iso"])
        ET.SubElement(cdrom, "target", dev="hda", bus="ide")
        ET.SubElement(cdrom, "readonly")

    # Network
    iface = ET.SubElement(devices, "interface", type="bridge")
    ET.SubElement(iface, "source", bridge=spec["network_bridge"])
    ET.SubElement(iface, "model", type="virtio")

    # Graphics (spice) + video
    ET.SubElement(devices, "graphics", type="vnc", autoport="yes")
    ET.SubElement(devices, "video").append(ET.Element("model", type="virtio"))

    # Console
    ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(devices, "console", type="pty")

    return ET.tostring(domain, encoding="unicode")


async def _ssh_run(
    host: str, user: str, port: int, ssh_key: str | None, command: str
) -> str:
    """Run a command on a remote host via SSH."""
    args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
    if ssh_key:
        args.extend(["-i", ssh_key])
    args.extend(["-p", str(port), f"{user}@{host}", command])
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"SSH command failed (rc={proc.returncode}): {stderr.decode()}"
        )
    return stdout.decode()


async def _provision_disk(
    host: str,
    user: str,
    port: int,
    ssh_key: str | None,
    disk_spec: dict,
    vm_name: str,
) -> str:
    """Provision a disk on the remote host. Returns the disk path."""
    disk_path = f"/var/lib/libvirt/images/{vm_name}.qcow2"
    if disk_spec["source"] == "create":
        size = disk_spec["size_gb"]
        cmd = f"sudo qemu-img create -f qcow2 {disk_path} {size}G"
    elif disk_spec["source"] == "copy":
        src = disk_spec["source_path"]
        cmd = f"sudo cp {src} {disk_path}"
    else:
        raise ValueError(f"Unknown disk source type: {disk_spec['source']}")
    await _ssh_run(host, user, port, ssh_key, cmd)
    return disk_path


def _parse_uri_parts(uri: str) -> tuple[str, str, int, str | None]:
    """Extract host, user, port, ssh_key from a qemu+ssh URI."""
    parsed = urllib.parse.urlparse(uri)
    host = parsed.hostname or ""
    user = parsed.username or getpass.getuser()
    port = parsed.port or 22
    qs = urllib.parse.parse_qs(parsed.query)
    ssh_key = qs.get("keyfile", [None])[0]
    if ssh_key:
        ssh_key = urllib.parse.unquote(ssh_key)
    return host, user, port, ssh_key


async def _find_isos(
    host: str,
    user: str,
    port: int,
    ssh_key: str | None,
    pattern: str,
) -> list[str]:
    """Find ISOs in /var/lib/libvirt/images/ matching pattern (all words, case-insensitive)."""
    output = await _ssh_run(
        host,
        user,
        port,
        ssh_key,
        "ls /var/lib/libvirt/images/*.iso 2>/dev/null || true",
    )
    all_isos = [line.strip() for line in output.splitlines() if line.strip()]
    words = pattern.lower().split()
    return [iso for iso in all_isos if all(w in iso.lower() for w in words)]


class CreateVMInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    name: str = Field(
        ...,
        description="VM name (required -- must be explicitly provided by the user, never auto-generated)",
        min_length=1,
        max_length=64,
    )
    template: str | None = Field(
        default=None, description="Template name (default: 'default')"
    )
    vcpus: int | None = Field(default=None, description="Override vCPUs", ge=1, le=256)
    memory_mb: int | None = Field(
        default=None, description="Override memory in MB", ge=64
    )
    disk_size_gb: int | None = Field(
        default=None, description="Override disk size in GB", ge=1
    )
    network_bridge: str | None = Field(
        default=None, description="Override bridge device"
    )
    boot_iso: str | None = Field(
        default=None, description="ISO path for boot/install media"
    )
    open_viewer: bool = Field(
        default=True, description="Auto-open virt-viewer console after creation"
    )


async def _launch_virt_viewer(uri: str, vm_name: str) -> bool:
    """Launch virt-viewer as a detached background process. Returns True on success."""
    try:
        await asyncio.create_subprocess_exec(
            "virt-viewer",
            "--wait",
            "--connect",
            uri,
            vm_name,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.warning("Failed to launch virt-viewer: %s", e)
        return False


@mcp.tool(
    name="libvirt_list_templates",
    annotations={
        "title": "List VM Templates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def libvirt_list_templates() -> str:
    """List available VM templates."""
    templates = sorted(TEMPLATES_DIR.glob("*.json"))
    if not templates:
        return "No templates found."
    lines = ["# VM Templates\n", "| Name | Description |", "|------|-------------|"]
    for path in templates:
        data = json.loads(path.read_text())
        name = path.stem
        desc = data.get("description", "")
        lines.append(f"| {name} | {desc} |")
    return "\n".join(lines)


@mcp.tool(
    name="libvirt_create_vm",
    annotations={
        "title": "Create VM from Template",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def libvirt_create_vm(params: CreateVMInput) -> str:
    """Create a new VM: provision storage, generate XML, define and start the domain.

    The 'name' parameter is mandatory and must be explicitly provided by the user.
    Do NOT invent or guess a name -- always ask the user if they have not specified one.
    """
    try:
        conn = _get_conn(params.alias)

        # 1. Load template + apply overrides
        tmpl = _load_template(params.template)
        spec = _apply_overrides(
            tmpl,
            vcpus=params.vcpus,
            memory_mb=params.memory_mb,
            disk_size_gb=params.disk_size_gb,
            network_bridge=params.network_bridge,
        )

        # 2. Provision disk on remote host
        uri = conn.getURI()
        host, user, port, ssh_key = _parse_uri_parts(uri)
        disk_path = await _provision_disk(
            host, user, port, ssh_key, spec["disk"], params.name
        )

        # 3. Build XML
        # 3. Resolve boot ISO
        boot_iso = params.boot_iso
        if boot_iso and not boot_iso.startswith("/"):
            matches = await _find_isos(host, user, port, ssh_key, boot_iso)
            if len(matches) == 1:
                boot_iso = matches[0]
            elif len(matches) > 1:
                iso_list = "\n".join(f"  - {m}" for m in matches)
                return f"Multiple ISOs match '{params.boot_iso}':\n{iso_list}\n\nPlease specify the exact path."
            else:
                all_isos = await _find_isos(host, user, port, ssh_key, "")
                if all_isos:
                    iso_list = "\n".join(f"  - {m}" for m in all_isos)
                    return f"No ISOs match '{params.boot_iso}'. Available ISOs:\n{iso_list}"
                return f"No ISOs match '{params.boot_iso}' and no ISOs found in /var/lib/libvirt/images/."

        xml_spec = {
            "name": params.name,
            "vcpus": spec["vcpus"],
            "memory_mb": spec["memory_mb"],
            "disk_path": disk_path,
            "disk_bus": spec["disk"].get("bus", "virtio"),
            "os": spec["os"],
            "network_bridge": spec.get("network_bridge", "br0"),
        }
        if boot_iso:
            xml_spec["boot_iso"] = boot_iso
            xml_spec["os"]["boot_dev"] = "cdrom"
        xml = _build_domain_xml(xml_spec)

        # 4. Define domain
        dom = await _run(lambda: conn.defineXML(xml))

        # 5. Start domain
        await _run(dom.create)

        # 6. Optionally launch virt-viewer
        viewer_msg = ""
        if params.open_viewer:
            if await _launch_virt_viewer(uri, params.name):
                viewer_msg = "\n  virt-viewer: launched"
            else:
                viewer_msg = "\n  virt-viewer: failed to launch (is it installed?)"

        return (
            f"VM '{params.name}' created and started on '{params.alias}'.\n"
            f"  UUID: {dom.UUIDString()}\n"
            f"  vCPUs: {spec['vcpus']}, Memory: {spec['memory_mb']} MB\n"
            f"  Disk: {disk_path}{viewer_msg}"
        )
    except Exception as e:
        return _format_error(e, f"creating VM '{params.name}'")


def _get_domain_disks(dom: libvirt.virDomain) -> list[str]:
    """Extract disk file paths from a domain's XML definition."""
    xml = dom.XMLDesc(0)
    root = ET.fromstring(xml)
    paths = []
    for source in root.findall(".//disk[@device='disk']/source"):
        f = source.get("file")
        if f:
            paths.append(f)
    return paths


class DeleteVmInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    domain: str = _DOMAIN_FIELD
    confirm: bool = Field(
        default=False,
        description="Must be set to true to actually delete. When false, returns a preview of what will be deleted.",
    )


@mcp.tool(
    name="libvirt_delete_vm",
    annotations={
        "title": "Delete VM (config + disks)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def libvirt_delete_vm(params: DeleteVmInput) -> str:
    """Permanently delete a VM: undefine the domain and remove its disk files.

    Call with confirm=false first to preview what will be deleted.
    Then call again with confirm=true to execute the deletion.
    """
    try:
        conn = _get_conn(params.alias)
        dom = await _run(lambda: _lookup_domain(conn, params.domain))
        name = dom.name()
        info = dom.info()
        state = _domain_state_str(info[0])
        disks = _get_domain_disks(dom)

        if not params.confirm:
            disk_list = "\n".join(f"  - {d}" for d in disks) if disks else "  (none)"
            return (
                f"DELETE PREVIEW for '{name}' on '{params.alias}':\n"
                f"  State: {state}\n"
                f"  Disk files to delete:\n{disk_list}\n\n"
                "To proceed, call again with confirm=true."
            )

        # Stop if running
        if info[0] in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED):
            await _run(dom.destroy)

        # Undefine
        await _run(dom.undefine)

        # Delete disk files via SSH
        uri = conn.getURI()
        host, user, port, ssh_key = _parse_uri_parts(uri)
        deleted = []
        errors = []
        for disk_path in disks:
            try:
                await _ssh_run(host, user, port, ssh_key, f"sudo rm -f {disk_path}")
                deleted.append(disk_path)
            except Exception as e:
                errors.append(f"{disk_path}: {e}")

        result = f"VM '{name}' deleted from '{params.alias}'.\n"
        if deleted:
            result += (
                "  Disks removed:\n" + "\n".join(f"    - {d}" for d in deleted) + "\n"
            )
        if errors:
            result += (
                "  Disk removal errors:\n"
                + "\n".join(f"    - {e}" for e in errors)
                + "\n"
            )
        if not disks:
            result += "  No disk files to remove.\n"
        return result
    except Exception as e:
        return _format_error(e, f"deleting VM '{params.domain}'")


def _rewrite_disk_paths(xml: str, path_map: dict[str, str]) -> str:
    """Rewrite disk source paths in domain XML and strip UUID."""
    root = ET.fromstring(xml)
    uuid_elem = root.find("uuid")
    if uuid_elem is not None:
        root.remove(uuid_elem)
    for source in root.findall(".//disk[@device='disk']/source"):
        f = source.get("file")
        if f and f in path_map:
            source.set("file", path_map[f])
    return ET.tostring(root, encoding="unicode")


async def _scp_between_hosts(
    src_host: str,
    src_user: str,
    src_port: int,
    src_key: str | None,
    dst_host: str,
    dst_user: str,
    dst_port: int,
    dst_key: str | None,
    src_path: str,
    dst_path: str,
) -> None:
    """Copy a file between two remote hosts. Tries direct scp, falls back to local relay."""
    # Try direct: ssh into source, scp to target
    scp_cmd = (
        f"sudo scp -o StrictHostKeyChecking=no "
        f"-P {dst_port} {src_path} {dst_user}@{dst_host}:{dst_path}"
    )
    try:
        await _ssh_run(src_host, src_user, src_port, src_key, scp_cmd)
        return
    except RuntimeError:
        logger.info("Direct scp failed, falling back to local relay")

    # Fallback: use shell pipe so the OS handles the plumbing
    # ssh source "sudo cat <path>" | ssh target "sudo tee <path> > /dev/null"
    src_ssh = "ssh -o StrictHostKeyChecking=no -o BatchMode=yes"
    if src_key:
        src_ssh += f" -i {src_key}"
    src_ssh += f" -p {src_port} {src_user}@{src_host} 'sudo cat {src_path}'"

    dst_ssh = "ssh -o StrictHostKeyChecking=no -o BatchMode=yes"
    if dst_key:
        dst_ssh += f" -i {dst_key}"
    dst_ssh += f" -p {dst_port} {dst_user}@{dst_host} 'sudo tee {dst_path} > /dev/null'"

    cmd = f"{src_ssh} | {dst_ssh}"
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Relay transfer failed: {stderr.decode()}")


class MigrateVMInput(BaseModel):
    model_config = _MODEL_CONFIG
    source_alias: str = Field(
        ...,
        description="Source host alias (must be connected)",
        min_length=1,
        max_length=64,
    )
    target_alias: str = Field(
        ...,
        description="Target host alias (must be connected)",
        min_length=1,
        max_length=64,
    )
    domain: str = _DOMAIN_FIELD
    shutdown_timeout_seconds: int = Field(
        default=30,
        description="Seconds to wait for graceful shutdown before force-stop",
        ge=1,
        le=3600,
    )
    disk_copy_timeout_seconds: int = Field(
        default=3600,
        description="Seconds allowed for each disk copy before failing migration",
        ge=30,
        le=86400,
    )
    confirm: bool = Field(
        default=False,
        description="false=migrate VM to target; true=clean up source after migration",
    )


class MigrationStatusInput(BaseModel):
    model_config = _MODEL_CONFIG
    job_id: str = Field(
        ..., description="Migration job ID", min_length=1, max_length=128
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _migration_job_create(params: MigrateVMInput) -> str:
    job_id = str(uuid4())
    async with _migration_jobs_lock:
        _migration_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "source_alias": params.source_alias,
            "target_alias": params.target_alias,
            "domain": params.domain,
            "created_at": _utc_now_iso(),
            "started_at": None,
            "finished_at": None,
            "phase": "queued",
            "phases": [{"phase": "queued", "at": _utc_now_iso()}],
            "result": None,
            "error": None,
        }
    return job_id


async def _migration_job_mark_phase(job_id: str, phase: str) -> None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return
        job["phase"] = phase
        job["phases"].append({"phase": phase, "at": _utc_now_iso()})


async def _migration_job_mark_running(job_id: str) -> None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return
        now = _utc_now_iso()
        job["status"] = "running"
        job["phase"] = "precheck"
        job["started_at"] = now
        job["phases"].append({"phase": "precheck", "at": now})


async def _migration_job_mark_success(job_id: str, result: str) -> None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return
        now = _utc_now_iso()
        job["status"] = "succeeded"
        job["phase"] = "done"
        job["finished_at"] = now
        job["result"] = result
        job["phases"].append({"phase": "done", "at": now})


async def _migration_job_mark_failure(job_id: str, error: str) -> None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return
        now = _utc_now_iso()
        job["status"] = "failed"
        job["phase"] = "failed"
        job["finished_at"] = now
        job["error"] = error
        job["phases"].append({"phase": "failed", "at": now})


async def _migration_job_get(job_id: str) -> dict | None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return None
        return copy.deepcopy(job)


async def _run_migration_job(job_id: str, params: MigrateVMInput) -> None:
    await _migration_job_mark_running(job_id)
    try:
        result = await _migrate_vm_offline(params, job_id)
    except Exception as e:
        await _migration_job_mark_failure(job_id, _format_error(e, "migrating VM"))
        return
    await _migration_job_mark_success(job_id, result)


async def _migrate_vm_offline(params: MigrateVMInput, job_id: str | None = None) -> str:
    src_conn = _get_conn(params.source_alias)
    tgt_conn = _get_conn(params.target_alias)

    if job_id:
        await _migration_job_mark_phase(job_id, "precheck")

    dom = await _run(lambda: _lookup_domain(src_conn, params.domain))
    name = dom.name()

    # Check domain state on target for idempotent retry handling.
    try:
        tgt_dom = await _run(lambda: tgt_conn.lookupByName(name))
        src_state = (await _run(dom.info))[0]
        tgt_state = (await _run(tgt_dom.info))[0]
        if src_state == libvirt.VIR_DOMAIN_SHUTOFF and tgt_state in (
            libvirt.VIR_DOMAIN_RUNNING,
            libvirt.VIR_DOMAIN_PAUSED,
        ):
            return (
                f"Migration already completed for '{name}'.\n"
                f"  Target '{params.target_alias}' is {_domain_state_str(tgt_state)}.\n"
                f"  Source '{params.source_alias}' is {_domain_state_str(src_state)}.\n"
                "Source cleanup is still pending. Call again with confirm=true to clean up source."
            )
        return (
            f"Error: Domain '{name}' already exists on target '{params.target_alias}'."
        )
    except libvirt.libvirtError:
        pass

    # Stop if running/paused.
    info = dom.info()
    if info[0] in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED):
        if job_id:
            await _migration_job_mark_phase(job_id, "shutdown")
        timeout_s = params.shutdown_timeout_seconds
        try:
            await _run(dom.shutdown)
            deadline = asyncio.get_running_loop().time() + timeout_s
            while True:
                state = (await _run(dom.info))[0]
                if state == libvirt.VIR_DOMAIN_SHUTOFF:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        f"domain still {_domain_state_str(state)} after {timeout_s}s"
                    )
                await asyncio.sleep(1)
        except Exception as shutdown_error:
            logger.info(
                "Graceful shutdown failed for '%s' (%s); force-stopping",
                name,
                shutdown_error,
            )
            await _run(dom.destroy)

    if job_id:
        await _migration_job_mark_phase(job_id, "collect_domain_xml")
    xml = await _run(dom.XMLDesc, libvirt.VIR_DOMAIN_XML_INACTIVE)
    disks = _get_domain_disks(dom)

    src_host, src_user, src_port, src_key = _parse_uri_parts(src_conn.getURI())
    dst_host, dst_user, dst_port, dst_key = _parse_uri_parts(tgt_conn.getURI())

    for disk_path in disks:
        if job_id:
            await _migration_job_mark_phase(job_id, f"copy_disk:{disk_path}")
        await asyncio.wait_for(
            _scp_between_hosts(
                src_host,
                src_user,
                src_port,
                src_key,
                dst_host,
                dst_user,
                dst_port,
                dst_key,
                disk_path,
                disk_path,
            ),
            timeout=params.disk_copy_timeout_seconds,
        )

    if job_id:
        await _migration_job_mark_phase(job_id, "define_target")
    path_map = {d: d for d in disks}
    new_xml = _rewrite_disk_paths(xml, path_map)
    new_dom = await _run(lambda: tgt_conn.defineXML(new_xml))

    if job_id:
        await _migration_job_mark_phase(job_id, "start_target")
    await _run(new_dom.create)

    disk_list = "\n".join(f"  - {d}" for d in disks) if disks else "  (none)"
    return (
        f"VM '{name}' migrated to '{params.target_alias}'.\n"
        f"  Disks transferred:\n{disk_list}\n"
        f"  UUID on target: {new_dom.UUIDString()}\n\n"
        f"Source still has the old definition and disk files.\n"
        f"Call again with confirm=true to clean up source."
    )


@mcp.tool(
    name="libvirt_migrate_vm",
    annotations={
        "title": "Migrate VM Between Hosts",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def libvirt_migrate_vm(params: MigrateVMInput) -> str:
    """Offline-migrate a VM from one host to another: stop, copy disks, define+start on target.

    Call with confirm=false to perform the migration.
    Then call with confirm=true to clean up the source (undefine + delete disks).
    """
    try:
        if not params.confirm:
            # --- Start async migration job ---
            _get_conn(params.source_alias)
            _get_conn(params.target_alias)
            src_conn = _get_conn(params.source_alias)
            await _run(lambda: _lookup_domain(src_conn, params.domain))
            job_id = await _migration_job_create(params)
            asyncio.create_task(_run_migration_job(job_id, params))
            return (
                f"Migration started for '{params.domain}' from '{params.source_alias}' to '{params.target_alias}'.\n"
                f"  Job ID: {job_id}\n"
                "Use libvirt_get_migration_status with this job_id to track progress."
            )
        else:
            # --- Clean up source ---
            src_conn = _get_conn(params.source_alias)
            dom = await _run(lambda: _lookup_domain(src_conn, params.domain))
            name = dom.name()
            info = dom.info()

            if info[0] in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED):
                return (
                    f"Error: Domain '{name}' is still running on source. Stop it first."
                )

            disks = _get_domain_disks(dom)
            await _run(dom.undefine)

            # Delete disk files via SSH
            src_host, src_user, src_port, src_key = _parse_uri_parts(src_conn.getURI())
            deleted = []
            errors = []
            for disk_path in disks:
                try:
                    await _ssh_run(
                        src_host, src_user, src_port, src_key, f"sudo rm -f {disk_path}"
                    )
                    deleted.append(disk_path)
                except Exception as e:
                    errors.append(f"{disk_path}: {e}")

            result = (
                f"Source cleanup complete for '{name}' on '{params.source_alias}'.\n"
            )
            if deleted:
                result += (
                    "  Disks removed:\n"
                    + "\n".join(f"    - {d}" for d in deleted)
                    + "\n"
                )
            if errors:
                result += (
                    "  Disk removal errors:\n"
                    + "\n".join(f"    - {e}" for e in errors)
                    + "\n"
                )
            if not disks:
                result += "  No disk files to remove.\n"
            return result

    except Exception as e:
        return _format_error(e, f"migrating VM '{params.domain}'")


@mcp.tool(
    name="libvirt_get_migration_status",
    annotations={
        "title": "Get VM Migration Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def libvirt_get_migration_status(params: MigrationStatusInput) -> str:
    """Get status details for a migration started by libvirt_migrate_vm(confirm=false)."""
    job = await _migration_job_get(params.job_id)
    if job is None:
        return f"Error: Migration job '{params.job_id}' not found."

    lines = [
        f"# Migration Job {job['job_id']}",
        "",
        f"- status: {job['status']}",
        f"- phase: {job['phase']}",
        f"- domain: {job['domain']}",
        f"- source: {job['source_alias']}",
        f"- target: {job['target_alias']}",
        f"- created_at: {job['created_at']}",
        f"- started_at: {job['started_at'] or '(pending)'}",
        f"- finished_at: {job['finished_at'] or '(pending)'}",
    ]
    if job["error"]:
        lines.append(f"- error: {job['error']}")
    if job["result"]:
        lines.extend(["", "## Result", job["result"]])
    lines.extend(["", "## Phase Timeline"])
    for entry in job["phases"]:
        lines.append(f"- {entry['at']}: {entry['phase']}")
    return "\n".join(lines)


@mcp.tool(
    name="libvirt_list_isos",
    annotations={
        "title": "List ISOs on Host",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def libvirt_list_isos(params: HostInput) -> str:
    """List all ISO files in /var/lib/libvirt/images/ on a connected host."""
    try:
        conn = _get_conn(params.alias)
        uri = conn.getURI()
        host, user, port, ssh_key = _parse_uri_parts(uri)
        isos = await _find_isos(host, user, port, ssh_key, "")
        if not isos:
            return (
                f"No ISO files found in /var/lib/libvirt/images/ on '{params.alias}'."
            )
        lines = [f"# ISOs on '{params.alias}'\n"]
        for iso in sorted(isos):
            lines.append(f"- {iso}")
        return "\n".join(lines)
    except Exception as e:
        return _format_error(e, "listing ISOs")


if __name__ == "__main__":
    mcp.run()
