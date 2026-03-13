import asyncio
import copy
import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from libvirt_mcp.app import mcp
from libvirt_mcp.common import _format_error, _run
from libvirt_mcp.connections import _get_conn
from libvirt_mcp.models import CreateVMInput, HostInput
from libvirt_mcp.remote import _find_isos, _parse_uri_parts, _ssh_run

logger = logging.getLogger("libvirt_mcp")
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


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
    ET.SubElement(domain, "currentMemory", unit="KiB").text = str(
        spec["memory_mb"] * 1024
    )
    ET.SubElement(domain, "vcpu").text = str(spec["vcpus"])

    os_elem = ET.SubElement(domain, "os")
    os_type_attrs = {"arch": spec["os"]["arch"]}
    if spec["os"]["arch"] == "x86_64":
        os_type_attrs["machine"] = "pc-q35-10.0"
    os_type = ET.SubElement(os_elem, "type", **os_type_attrs)
    os_type.text = spec["os"]["type"]
    ET.SubElement(os_elem, "boot", dev=spec["os"]["boot_dev"])

    if spec["os"]["arch"] == "x86_64":
        features = ET.SubElement(domain, "features")
        ET.SubElement(features, "acpi")
        ET.SubElement(features, "apic")
        ET.SubElement(
            domain,
            "cpu",
            mode="host-passthrough",
            check="none",
            migratable="on",
        )
        clock = ET.SubElement(domain, "clock", offset="utc")
        ET.SubElement(clock, "timer", name="rtc", tickpolicy="catchup")
        ET.SubElement(clock, "timer", name="pit", tickpolicy="delay")
        ET.SubElement(clock, "timer", name="hpet", present="no")
    else:
        ET.SubElement(domain, "clock", offset="utc")

    ET.SubElement(domain, "on_poweroff").text = "destroy"
    ET.SubElement(domain, "on_reboot").text = "restart"
    ET.SubElement(domain, "on_crash").text = "destroy"

    if spec["os"]["arch"] == "x86_64":
        pm = ET.SubElement(domain, "pm")
        ET.SubElement(pm, "suspend-to-mem", enabled="no")
        ET.SubElement(pm, "suspend-to-disk", enabled="no")

    devices = ET.SubElement(domain, "devices")
    ET.SubElement(devices, "emulator").text = "/usr/bin/qemu-system-x86_64"

    disk = ET.SubElement(devices, "disk", type="file", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", file=spec["disk_path"])
    ET.SubElement(disk, "target", dev="vda", bus=spec["disk_bus"])
    if spec["os"]["arch"] == "x86_64":
        ET.SubElement(
            disk,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x04",
            slot="0x00",
            function="0x0",
        )

    if spec.get("boot_iso"):
        cdrom = ET.SubElement(devices, "disk", type="file", device="cdrom")
        ET.SubElement(cdrom, "driver", name="qemu", type="raw")
        ET.SubElement(cdrom, "source", file=spec["boot_iso"])
        ET.SubElement(cdrom, "target", dev="hda", bus="ide")
        ET.SubElement(cdrom, "readonly")

    if spec["os"]["arch"] == "x86_64":
        usb = ET.SubElement(
            devices, "controller", type="usb", index="0", model="qemu-xhci", ports="15"
        )
        ET.SubElement(
            usb,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x02",
            slot="0x00",
            function="0x0",
        )
        ET.SubElement(devices, "controller", type="pci", index="0", model="pcie-root")

        for index, chassis, port, slot, function, multifunction in [
            (1, 1, "0x10", "0x02", "0x0", True),
            (2, 2, "0x11", "0x02", "0x1", False),
            (3, 3, "0x12", "0x02", "0x2", False),
            (4, 4, "0x13", "0x02", "0x3", False),
            (5, 5, "0x14", "0x02", "0x4", False),
            (6, 6, "0x15", "0x02", "0x5", False),
            (7, 7, "0x16", "0x02", "0x6", False),
            (8, 8, "0x17", "0x02", "0x7", False),
            (9, 9, "0x18", "0x03", "0x0", True),
            (10, 10, "0x19", "0x03", "0x1", False),
            (11, 11, "0x1a", "0x03", "0x2", False),
            (12, 12, "0x1b", "0x03", "0x3", False),
            (13, 13, "0x1c", "0x03", "0x4", False),
            (14, 14, "0x1d", "0x03", "0x5", False),
        ]:
            controller = ET.SubElement(
                devices,
                "controller",
                type="pci",
                index=str(index),
                model="pcie-root-port",
            )
            ET.SubElement(controller, "model", name="pcie-root-port")
            ET.SubElement(controller, "target", chassis=str(chassis), port=port)
            address_attrs = {
                "type": "pci",
                "domain": "0x0000",
                "bus": "0x00",
                "slot": slot,
                "function": function,
            }
            if multifunction:
                address_attrs["multifunction"] = "on"
            ET.SubElement(controller, "address", **address_attrs)

        sata = ET.SubElement(devices, "controller", type="sata", index="0")
        ET.SubElement(
            sata,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x00",
            slot="0x1f",
            function="0x2",
        )
        virtio_serial = ET.SubElement(
            devices, "controller", type="virtio-serial", index="0"
        )
        ET.SubElement(
            virtio_serial,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x03",
            slot="0x00",
            function="0x0",
        )
    else:
        ET.SubElement(devices, "controller", type="pci", index="0", model="pci-root")

    iface = ET.SubElement(devices, "interface", type="bridge")
    ET.SubElement(iface, "source", bridge=spec["network_bridge"])
    ET.SubElement(iface, "model", type="virtio")
    if spec["os"]["arch"] == "x86_64":
        ET.SubElement(
            iface,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x01",
            slot="0x00",
            function="0x0",
        )

    serial = ET.SubElement(devices, "serial", type="pty")
    serial_target = ET.SubElement(serial, "target", type="isa-serial", port="0")
    ET.SubElement(serial_target, "model", name="isa-serial")

    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(console, "target", type="serial", port="0")

    if spec["os"]["arch"] == "x86_64":
        channel = ET.SubElement(devices, "channel", type="unix")
        ET.SubElement(channel, "target", type="virtio", name="org.qemu.guest_agent.0")
        ET.SubElement(
            channel,
            "address",
            type="virtio-serial",
            controller="0",
            bus="0",
            port="1",
        )
        tablet = ET.SubElement(devices, "input", type="tablet", bus="usb")
        ET.SubElement(tablet, "address", type="usb", bus="0", port="1")

    ET.SubElement(devices, "input", type="mouse", bus="ps2")
    ET.SubElement(devices, "input", type="keyboard", bus="ps2")

    graphics = ET.SubElement(devices, "graphics", type="vnc", port="-1", autoport="yes")
    ET.SubElement(graphics, "listen", type="address")
    ET.SubElement(devices, "audio", id="1", type="none")

    video = ET.SubElement(devices, "video")
    ET.SubElement(video, "model", type="virtio", heads="1", primary="yes")
    if spec["os"]["arch"] == "x86_64":
        ET.SubElement(
            video,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x00",
            slot="0x01",
            function="0x0",
        )

    if spec["os"]["arch"] == "x86_64":
        ET.SubElement(devices, "watchdog", model="itco", action="reset")

    memballoon = ET.SubElement(devices, "memballoon", model="virtio")
    if spec["os"]["arch"] == "x86_64":
        ET.SubElement(
            memballoon,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x05",
            slot="0x00",
            function="0x0",
        )
        rng = ET.SubElement(devices, "rng", model="virtio")
        ET.SubElement(rng, "backend", model="random").text = "/dev/urandom"
        ET.SubElement(
            rng,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x06",
            slot="0x00",
            function="0x0",
        )

    return ET.tostring(domain, encoding="unicode")


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


@mcp.tool(name="libvirt_list_templates")
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


@mcp.tool(name="libvirt_create_vm")
async def libvirt_create_vm(params: CreateVMInput) -> str:
    """Create a new VM: provision storage, generate XML, define and start the domain.

    The 'name' parameter is mandatory and must be explicitly provided by the user.
    Do NOT invent or guess a name -- always ask the user if they have not specified one.
    """
    try:
        conn = _get_conn(params.alias)
        tmpl = _load_template(params.template)
        spec = _apply_overrides(
            tmpl,
            vcpus=params.vcpus,
            memory_mb=params.memory_mb,
            disk_size_gb=params.disk_size_gb,
            network_bridge=params.network_bridge,
        )

        uri = conn.getURI()
        host, user, port, ssh_key = _parse_uri_parts(uri)
        disk_path = await _provision_disk(
            host, user, port, ssh_key, spec["disk"], params.name
        )

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

        dom = await _run(lambda: conn.defineXML(xml))
        await _run(dom.create)

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


@mcp.tool(name="libvirt_list_isos")
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
