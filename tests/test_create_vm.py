"""Unit tests for the create VM feature: templates, XML generation, disk provisioning, and the tool."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import libvirt
import pytest

import server
from server import (
    _apply_overrides,
    _build_domain_xml,
    _find_isos,
    _launch_virt_viewer,
    _load_template,
    _provision_disk,
    _ssh_run,
    libvirt_create_vm,
    libvirt_list_templates,
    CreateVMInput,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@pytest.fixture(autouse=True)
def clear_connections():
    server._connections.clear()
    yield
    server._connections.clear()


def make_mock_conn():
    conn = MagicMock(spec=libvirt.virConnect)
    conn.getVersion.return_value = 9007001
    conn.getHostname.return_value = "testhost"
    conn.getLibVersion.return_value = 10002000
    conn.getURI.return_value = "qemu+ssh://root@testhost:22/system"
    return conn


def make_mock_domain(name="test-vm", uuid="aaaaaaaa-0000-0000-0000-000000000001"):
    dom = MagicMock(spec=libvirt.virDomain)
    dom.name.return_value = name
    dom.UUIDString.return_value = uuid
    dom.info.return_value = [libvirt.VIR_DOMAIN_RUNNING, 2097152, 1048576, 2, 0]
    dom.isPersistent.return_value = 1
    dom.autostart.return_value = 0
    return dom


class TestLoadTemplate:
    def test_load_default(self):
        tmpl = _load_template("default")
        assert tmpl["vcpus"] == 1
        assert tmpl["memory_mb"] == 1024
        assert tmpl["disk"]["source"] == "create"

    def test_load_suse_leap_micro(self):
        tmpl = _load_template("suse-leap-micro")
        assert tmpl["vcpus"] == 2
        assert tmpl["disk"]["source"] == "copy"
        assert "openSUSE" in tmpl["disk"]["source_path"]

    def test_missing_template(self):
        with pytest.raises(FileNotFoundError, match="nonexistent"):
            _load_template("nonexistent")

    def test_default_template_when_none(self):
        tmpl = _load_template(None)
        assert tmpl["vcpus"] == 1


class TestApplyOverrides:
    def test_override_vcpus(self):
        tmpl = {
            "vcpus": 1,
            "memory_mb": 1024,
            "disk": {"source": "create", "size_gb": 10},
        }
        result = _apply_overrides(tmpl, vcpus=4)
        assert result["vcpus"] == 4
        assert result["memory_mb"] == 1024

    def test_override_memory(self):
        tmpl = {"vcpus": 1, "memory_mb": 1024}
        result = _apply_overrides(tmpl, memory_mb=2048)
        assert result["memory_mb"] == 2048

    def test_override_disk_size(self):
        tmpl = {"disk": {"source": "create", "size_gb": 10}}
        result = _apply_overrides(tmpl, disk_size_gb=20)
        assert result["disk"]["size_gb"] == 20

    def test_override_network(self):
        tmpl = {"network_bridge": "br0"}
        result = _apply_overrides(tmpl, network_bridge="virbr0")
        assert result["network_bridge"] == "virbr0"

    def test_no_overrides(self):
        tmpl = {"vcpus": 2, "memory_mb": 1024}
        result = _apply_overrides(tmpl)
        assert result == tmpl

    def test_override_does_not_mutate_original(self):
        tmpl = {
            "vcpus": 1,
            "memory_mb": 1024,
            "disk": {"source": "create", "size_gb": 10},
        }
        _apply_overrides(tmpl, vcpus=4, disk_size_gb=20)
        assert tmpl["vcpus"] == 1
        assert tmpl["disk"]["size_gb"] == 10


class TestListTemplates:
    async def test_lists_templates(self):
        result = await libvirt_list_templates()
        assert "default" in result
        assert "suse-leap-micro" in result
        assert "Basic KVM" in result


class TestBuildDomainXml:
    def _parse(self, xml_str):
        return ET.fromstring(xml_str)

    def _find_required(self, root, path):
        node = root.find(path)
        assert node is not None, path
        return node

    def _base_spec(self, **overrides):
        spec = {
            "name": "myvm",
            "vcpus": 2,
            "memory_mb": 2048,
            "disk_path": "/var/lib/libvirt/images/myvm.qcow2",
            "disk_bus": "virtio",
            "os": {"type": "hvm", "arch": "x86_64", "boot_dev": "hd"},
            "network_bridge": "br0",
        }
        spec.update(overrides)
        return spec

    def test_basic_spec(self):
        spec = self._base_spec()
        xml = _build_domain_xml(spec)
        root = self._parse(xml)
        assert root.tag == "domain"
        assert root.get("type") == "kvm"
        assert self._find_required(root, "name").text == "myvm"
        assert self._find_required(root, "vcpu").text == "2"
        assert self._find_required(root, "memory").text == "2097152"  # 2048 * 1024
        assert self._find_required(root, "memory").get("unit") == "KiB"
        assert self._find_required(root, "currentMemory").text == "2097152"
        disk = self._find_required(root, ".//disk")
        assert (
            self._find_required(disk, "source").get("file")
            == "/var/lib/libvirt/images/myvm.qcow2"
        )
        assert self._find_required(disk, "target").get("bus") == "virtio"
        iface = self._find_required(root, ".//interface")
        assert self._find_required(iface, "source").get("bridge") == "br0"
        assert self._find_required(root, "./os/type").get("machine") == "pc-q35-10.0"
        cpu = self._find_required(root, "cpu")
        assert cpu.get("mode") == "host-passthrough"
        assert cpu.get("migratable") == "on"
        assert root.find("./features/acpi") is not None
        assert root.find("./features/apic") is not None
        assert self._find_required(root, "./pm/suspend-to-mem").get("enabled") == "no"
        assert self._find_required(root, "./pm/suspend-to-disk").get("enabled") == "no"
        assert (
            self._find_required(root, "./devices/controller[@type='usb']").get("model")
            == "qemu-xhci"
        )
        assert root.find("./devices/controller[@type='virtio-serial']") is not None
        assert (
            self._find_required(root, "./devices/channel/target").get("name")
            == "org.qemu.guest_agent.0"
        )
        assert self._find_required(root, "./devices/watchdog").get("model") == "itco"
        assert self._find_required(root, "./devices/rng/backend").text == "/dev/urandom"
        assert len(root.findall("./devices/controller[@model='pcie-root-port']")) == 14

    def test_with_boot_iso(self):
        spec = self._base_spec(
            vcpus=1,
            memory_mb=1024,
            os={"type": "hvm", "arch": "x86_64", "boot_dev": "cdrom"},
            boot_iso="/var/lib/libvirt/images/install.iso",
        )
        xml = _build_domain_xml(spec)
        root = self._parse(xml)
        cdroms = [d for d in root.findall(".//disk") if d.get("device") == "cdrom"]
        assert len(cdroms) == 1
        assert (
            self._find_required(cdroms[0], "source").get("file")
            == "/var/lib/libvirt/images/install.iso"
        )
        boot = self._find_required(root, ".//os/boot")
        assert boot.get("dev") == "cdrom"

    def test_different_arch(self):
        spec = self._base_spec(
            vcpus=1,
            memory_mb=512,
            os={"type": "hvm", "arch": "aarch64", "boot_dev": "hd"},
        )
        xml = _build_domain_xml(spec)
        root = self._parse(xml)
        assert self._find_required(root, ".//os/type").get("arch") == "aarch64"
        assert self._find_required(root, ".//os/type").get("machine") is None
        assert root.find("cpu") is None
        assert root.find("./devices/controller[@type='virtio-serial']") is None


class TestProvisionDisk:
    async def test_create_mode(self):
        disk_spec = {"source": "create", "size_gb": 10}
        with patch(
            "libvirt_mcp.create_vm._ssh_run", new_callable=AsyncMock, return_value=""
        ) as mock_ssh:
            await _provision_disk("myhost", "root", 22, None, disk_spec, "myvm")
        cmd = mock_ssh.call_args[0][4]
        assert "sudo qemu-img create" in cmd
        assert "10G" in cmd
        assert "myvm.qcow2" in cmd

    async def test_copy_mode(self):
        disk_spec = {"source": "copy", "source_path": "/images/base.qcow2"}
        with patch(
            "libvirt_mcp.create_vm._ssh_run", new_callable=AsyncMock, return_value=""
        ) as mock_ssh:
            await _provision_disk("myhost", "root", 22, None, disk_spec, "myvm")
        cmd = mock_ssh.call_args[0][4]
        assert "sudo cp" in cmd
        assert "/images/base.qcow2" in cmd
        assert "myvm.qcow2" in cmd

    async def test_ssh_failure(self):
        disk_spec = {"source": "create", "size_gb": 10}
        with patch(
            "libvirt_mcp.create_vm._ssh_run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("ssh failed"),
        ):
            with pytest.raises(RuntimeError, match="ssh failed"):
                await _provision_disk("myhost", "root", 22, None, disk_spec, "myvm")


class TestSshRun:
    async def test_success(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0
        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            result = await _ssh_run("myhost", "testuser", 22, None, "echo ok")
        assert result == "ok\n"
        args = mock_exec.call_args[0]
        assert args[0] == "ssh"
        assert "testuser@myhost" in args

    async def test_with_key(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0
        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            await _ssh_run("myhost", "root", 22, "/path/to/key", "echo ok")
        args = mock_exec.call_args[0]
        assert "-i" in args
        assert "/path/to/key" in args

    async def test_failure(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"Permission denied\n")
        mock_proc.returncode = 255
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="Permission denied"):
                await _ssh_run("myhost", "root", 22, None, "ls")


class TestCreateVm:
    def setup_method(self):
        self.conn = make_mock_conn()
        server._connections["lab"] = self.conn

    async def test_happy_path(self):
        dom = make_mock_domain(name="newvm")
        self.conn.defineXML.return_value = dom
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            result = await libvirt_create_vm(CreateVMInput(alias="lab", name="newvm"))
        assert "newvm" in result
        assert (
            "defined" in result.lower()
            or "started" in result.lower()
            or "created" in result.lower()
        )
        dom.create.assert_called_once()

    async def test_with_overrides(self):
        dom = make_mock_domain(name="bigvm")
        self.conn.defineXML.return_value = dom
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            result = await libvirt_create_vm(
                CreateVMInput(
                    alias="lab", name="bigvm", vcpus=4, memory_mb=4096, disk_size_gb=50
                )
            )
        assert "bigvm" in result
        assert "4096 MB" in result

    async def test_with_boot_iso(self):
        dom = make_mock_domain(name="installvm")
        self.conn.defineXML.return_value = dom
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            with patch(
                "libvirt_mcp.create_vm._build_domain_xml",
                wraps=server._build_domain_xml,
            ) as mock_xml:
                result = await libvirt_create_vm(
                    CreateVMInput(
                        alias="lab", name="installvm", boot_iso="/images/install.iso"
                    )
                )
                spec_arg = mock_xml.call_args[0][0]
                assert spec_arg["boot_iso"] == "/images/install.iso"
        assert "installvm" in result

    async def test_template_not_found(self):
        result = await libvirt_create_vm(
            CreateVMInput(alias="lab", name="newvm", template="nonexistent")
        )
        assert "Error" in result

    async def test_disk_provision_fails(self):
        with patch(
            "libvirt_mcp.create_vm._provision_disk",
            new_callable=AsyncMock,
            side_effect=RuntimeError("disk error"),
        ):
            result = await libvirt_create_vm(CreateVMInput(alias="lab", name="newvm"))
        assert "Error" in result

    async def test_define_xml_fails(self):
        self.conn.defineXML.side_effect = libvirt.libvirtError("bad XML")
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            result = await libvirt_create_vm(CreateVMInput(alias="lab", name="newvm"))
        assert "Error" in result

    async def test_open_viewer_default(self):
        dom = make_mock_domain(name="newvm")
        self.conn.defineXML.return_value = dom
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            with patch(
                "libvirt_mcp.create_vm._launch_virt_viewer",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_viewer:
                result = await libvirt_create_vm(
                    CreateVMInput(alias="lab", name="newvm")
                )
        mock_viewer.assert_called_once()
        assert "virt-viewer: launched" in result

    async def test_open_viewer_false(self):
        dom = make_mock_domain(name="newvm")
        self.conn.defineXML.return_value = dom
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            with patch(
                "libvirt_mcp.create_vm._launch_virt_viewer", new_callable=AsyncMock
            ) as mock_viewer:
                result = await libvirt_create_vm(
                    CreateVMInput(alias="lab", name="newvm", open_viewer=False)
                )
        mock_viewer.assert_not_called()
        assert "virt-viewer" not in result

    async def test_viewer_failure_does_not_fail_create(self):
        dom = make_mock_domain(name="newvm")
        self.conn.defineXML.return_value = dom
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            with patch(
                "libvirt_mcp.create_vm._launch_virt_viewer",
                new_callable=AsyncMock,
                return_value=False,
            ):
                result = await libvirt_create_vm(
                    CreateVMInput(alias="lab", name="newvm")
                )
        assert "created" in result.lower()
        assert "failed to launch" in result


class TestLaunchVirtViewer:
    async def test_builds_correct_command(self):
        mock_proc = AsyncMock()
        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            result = await _launch_virt_viewer("qemu+ssh://root@host/system", "myvm")
        assert result is True
        args = mock_exec.call_args[0]
        assert args == (
            "virt-viewer",
            "--wait",
            "--connect",
            "qemu+ssh://root@host/system",
            "myvm",
        )

    async def test_returns_false_on_failure(self):
        with patch(
            "asyncio.create_subprocess_exec", side_effect=FileNotFoundError("not found")
        ):
            result = await _launch_virt_viewer("qemu+ssh://root@host/system", "myvm")
        assert result is False


ISO_LS_OUTPUT = (
    "/var/lib/libvirt/images/debian-12.8.0-amd64-netinst.iso\n"
    "/var/lib/libvirt/images/debian-11.9.0-amd64-netinst.iso\n"
    "/var/lib/libvirt/images/ubuntu-24.04-server.iso\n"
)


class TestFindIsos:
    async def test_single_match(self):
        with patch(
            "libvirt_mcp.remote._ssh_run",
            new_callable=AsyncMock,
            return_value=ISO_LS_OUTPUT,
        ):
            result = await _find_isos("host", "root", 22, None, "debian 12")
        assert result == ["/var/lib/libvirt/images/debian-12.8.0-amd64-netinst.iso"]

    async def test_multiple_matches(self):
        with patch(
            "libvirt_mcp.remote._ssh_run",
            new_callable=AsyncMock,
            return_value=ISO_LS_OUTPUT,
        ):
            result = await _find_isos("host", "root", 22, None, "debian")
        assert len(result) == 2
        assert all("debian" in r.lower() for r in result)

    async def test_no_match(self):
        with patch(
            "libvirt_mcp.remote._ssh_run",
            new_callable=AsyncMock,
            return_value=ISO_LS_OUTPUT,
        ):
            result = await _find_isos("host", "root", 22, None, "fedora")
        assert result == []

    async def test_empty_pattern_returns_all(self):
        with patch(
            "libvirt_mcp.remote._ssh_run",
            new_callable=AsyncMock,
            return_value=ISO_LS_OUTPUT,
        ):
            result = await _find_isos("host", "root", 22, None, "")
        assert len(result) == 3


class TestCreateVmIsoDiscovery:
    def setup_method(self):
        self.conn = make_mock_conn()
        server._connections["lab"] = self.conn

    async def test_iso_discovery_single(self):
        dom = make_mock_domain(name="newvm")
        self.conn.defineXML.return_value = dom
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            with patch(
                "libvirt_mcp.create_vm._find_isos",
                new_callable=AsyncMock,
                return_value=["/var/lib/libvirt/images/debian-12.iso"],
            ):
                with patch(
                    "libvirt_mcp.create_vm._build_domain_xml",
                    wraps=server._build_domain_xml,
                ) as mock_xml:
                    result = await libvirt_create_vm(
                        CreateVMInput(alias="lab", name="newvm", boot_iso="debian 12")
                    )
                    spec_arg = mock_xml.call_args[0][0]
                    assert (
                        spec_arg["boot_iso"] == "/var/lib/libvirt/images/debian-12.iso"
                    )
        assert "created" in result.lower()

    async def test_iso_discovery_multiple(self):
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            with patch(
                "libvirt_mcp.create_vm._find_isos",
                new_callable=AsyncMock,
                return_value=[
                    "/var/lib/libvirt/images/debian-12.iso",
                    "/var/lib/libvirt/images/debian-12.1.iso",
                ],
            ):
                result = await libvirt_create_vm(
                    CreateVMInput(alias="lab", name="newvm", boot_iso="debian 12")
                )
        assert "Multiple ISOs" in result
        assert "debian-12.iso" in result
        assert "debian-12.1.iso" in result
        self.conn.defineXML.assert_not_called()

    async def test_iso_discovery_no_match(self):
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            with patch(
                "libvirt_mcp.create_vm._find_isos",
                new_callable=AsyncMock,
                side_effect=[
                    [],  # first call: no matches for pattern
                    ["/var/lib/libvirt/images/ubuntu.iso"],  # second call: list all
                ],
            ):
                result = await libvirt_create_vm(
                    CreateVMInput(alias="lab", name="newvm", boot_iso="fedora")
                )
        assert "No ISOs match" in result
        assert "ubuntu.iso" in result
        self.conn.defineXML.assert_not_called()

    async def test_absolute_iso_path(self):
        dom = make_mock_domain(name="newvm")
        self.conn.defineXML.return_value = dom
        with patch("libvirt_mcp.create_vm._provision_disk", new_callable=AsyncMock):
            with patch(
                "libvirt_mcp.create_vm._find_isos", new_callable=AsyncMock
            ) as mock_find:
                with patch(
                    "libvirt_mcp.create_vm._build_domain_xml",
                    wraps=server._build_domain_xml,
                ) as mock_xml:
                    result = await libvirt_create_vm(
                        CreateVMInput(
                            alias="lab", name="newvm", boot_iso="/images/install.iso"
                        )
                    )
                    spec_arg = mock_xml.call_args[0][0]
                    assert spec_arg["boot_iso"] == "/images/install.iso"
        mock_find.assert_not_called()
        assert "created" in result.lower()
