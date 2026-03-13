import xml.etree.ElementTree as ET

import libvirt

from libvirt_mcp.app import mcp
from libvirt_mcp.common import _domain_state_str, _format_error, _run
from libvirt_mcp.connections import _get_conn
from libvirt_mcp.domains import _lookup_domain
from libvirt_mcp.models import DeleteVmInput
from libvirt_mcp.remote import _parse_uri_parts, _ssh_run


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


@mcp.tool(name="libvirt_delete_vm")
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

        if info[0] in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED):
            await _run(dom.destroy)

        await _run(dom.undefine)

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
