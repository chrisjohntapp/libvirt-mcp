import json

import libvirt

from libvirt_mcp.app import mcp
from libvirt_mcp.common import _domain_state_str, _format_error, _run
from libvirt_mcp.connections import _get_conn
from libvirt_mcp.models import (
    DefineVMInput,
    DomainInfoInput,
    DomainInput,
    ListDomainsInput,
    ResponseFormat,
)


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


def _domain_summary(dom: libvirt.virDomain) -> dict:
    """Return a concise dict summary of a domain."""
    info = dom.info()
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


@mcp.tool(name="libvirt_list_domains")
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


@mcp.tool(name="libvirt_get_domain_info")
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


@mcp.tool(name="libvirt_get_domain_xml")
async def libvirt_get_domain_xml(params: DomainInput) -> str:
    """Retrieve the full XML definition of a domain."""
    try:
        conn = _get_conn(params.alias)
        dom = await _run(lambda: _lookup_domain(conn, params.domain))
        return await _run(dom.XMLDesc, 0)
    except Exception as e:
        return _format_error(e, f"get XML for '{params.domain}'")


@mcp.tool(name="libvirt_start_domain")
async def libvirt_start_domain(params: DomainInput) -> str:
    """Start (boot) a shutoff domain."""
    return await _domain_action(params, "create", "Domain started")


@mcp.tool(name="libvirt_shutdown_domain")
async def libvirt_shutdown_domain(params: DomainInput) -> str:
    """Send an ACPI shutdown signal to a running domain (graceful shutdown).
    Use libvirt_destroy_domain to force-stop immediately if needed."""
    return await _domain_action(params, "shutdown", "Shutdown signal sent to")


@mcp.tool(name="libvirt_destroy_domain")
async def libvirt_destroy_domain(params: DomainInput) -> str:
    """Forcefully stop a running domain immediately. WARNING: not a graceful shutdown."""
    return await _domain_action(params, "destroy", "Domain force-stopped")


@mcp.tool(name="libvirt_reboot_domain")
async def libvirt_reboot_domain(params: DomainInput) -> str:
    """Send a graceful reboot signal to a running domain."""
    return await _domain_action(params, "reboot", "Reboot signal sent to")


@mcp.tool(name="libvirt_suspend_domain")
async def libvirt_suspend_domain(params: DomainInput) -> str:
    """Suspend (pause) a running domain. Use libvirt_resume_domain to unpause."""
    return await _domain_action(params, "suspend", "Domain suspended")


@mcp.tool(name="libvirt_resume_domain")
async def libvirt_resume_domain(params: DomainInput) -> str:
    """Resume a suspended (paused) domain."""
    return await _domain_action(params, "resume", "Domain resumed")


@mcp.tool(name="libvirt_define_domain")
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


@mcp.tool(name="libvirt_undefine_domain")
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
