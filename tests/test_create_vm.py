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
        tmpl = {"vcpus": 1, "memory_mb": 1024, "disk": {"source": "create", "size_gb": 10}}
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
        tmpl = {"vcpus": 1, "memory_mb": 1024, "disk": {"source": "create", "size_gb": 10}}
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

    def test_basic_spec(self):
        spec = {
            "name": "myvm",
            "vcpus": 2,
            "memory_mb": 2048,
            "disk_path": "/var/lib/libvirt/images/myvm.qcow2",
            "disk_bus": "virtio",
            "os": {"type": "hvm", "arch": "x86_64", "boot_dev": "hd"},
            "network_bridge": "br0",
        }
        xml = _build_domain_xml(spec)
        root = self._parse(xml)
        assert root.tag == "domain"
        assert root.get("type") == "kvm"
        assert root.find("name").text == "myvm"
        assert root.find("vcpu").text == "2"
        assert root.find("memory").text == "2097152"  # 2048 * 1024
        assert root.find("memory").get("unit") == "KiB"
        disk = root.find(".//disk")
        assert disk.find("source").get("file") == "/var/lib/libvirt/images/myvm.qcow2"
        assert disk.find("target").get("bus") == "virtio"
        iface = root.find(".//interface")
        assert iface.find("source").get("bridge") == "br0"

    def test_with_boot_iso(self):
        spec = {
            "name": "myvm",
            "vcpus": 1,
            "memory_mb": 1024,
            "disk_path": "/var/lib/libvirt/images/myvm.qcow2",
            "disk_bus": "virtio",
            "os": {"type": "hvm", "arch": "x86_64", "boot_dev": "cdrom"},
            "network_bridge": "br0",
            "boot_iso": "/var/lib/libvirt/images/install.iso",
        }
        xml = _build_domain_xml(spec)
        root = self._parse(xml)
        cdroms = [d for d in root.findall(".//disk") if d.get("device") == "cdrom"]
        assert len(cdroms) == 1
        assert cdroms[0].find("source").get("file") == "/var/lib/libvirt/images/install.iso"
        boot = root.find(".//os/boot")
        assert boot.get("dev") == "cdrom"

    def test_different_arch(self):
        spec = {
            "name": "myvm",
            "vcpus": 1,
            "memory_mb": 512,
            "disk_path": "/var/lib/libvirt/images/myvm.qcow2",
            "disk_bus": "virtio",
            "os": {"type": "hvm", "arch": "aarch64", "boot_dev": "hd"},
            "network_bridge": "br0",
        }
        xml = _build_domain_xml(spec)
        root = self._parse(xml)
        assert root.find(".//os/type").get("arch") == "aarch64"


class TestProvisionDisk:
    async def test_create_mode(self):
        disk_spec = {"source": "create", "size_gb": 10}
        with patch("server._ssh_run", new_callable=AsyncMock, return_value="") as mock_ssh:
            await _provision_disk("myhost", "root", 22, None, disk_spec, "myvm")
        cmd = mock_ssh.call_args[0][4]
        assert "sudo qemu-img create" in cmd
        assert "10G" in cmd
        assert "myvm.qcow2" in cmd

    async def test_copy_mode(self):
        disk_spec = {"source": "copy", "source_path": "/images/base.qcow2"}
        with patch("server._ssh_run", new_callable=AsyncMock, return_value="") as mock_ssh:
            await _provision_disk("myhost", "root", 22, None, disk_spec, "myvm")
        cmd = mock_ssh.call_args[0][4]
        assert "sudo cp" in cmd
        assert "/images/base.qcow2" in cmd
        assert "myvm.qcow2" in cmd

    async def test_ssh_failure(self):
        disk_spec = {"source": "create", "size_gb": 10}
        with patch("server._ssh_run", new_callable=AsyncMock, side_effect=RuntimeError("ssh failed")):
            with pytest.raises(RuntimeError, match="ssh failed"):
                await _provision_disk("myhost", "root", 22, None, disk_spec, "myvm")


class TestSshRun:
    async def test_success(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await _ssh_run("myhost", "testuser", 22, None, "echo ok")
        assert result == "ok\n"
        args = mock_exec.call_args[0]
        assert args[0] == "ssh"
        assert "testuser@myhost" in args

    async def test_with_key(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
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
        with patch("server._provision_disk", new_callable=AsyncMock):
            result = await libvirt_create_vm(
                CreateVMInput(alias="lab", name="newvm")
            )
        assert "newvm" in result
        assert "defined" in result.lower() or "started" in result.lower() or "created" in result.lower()
        dom.create.assert_called_once()

    async def test_with_overrides(self):
        dom = make_mock_domain(name="bigvm")
        self.conn.defineXML.return_value = dom
        with patch("server._provision_disk", new_callable=AsyncMock):
            result = await libvirt_create_vm(
                CreateVMInput(alias="lab", name="bigvm", vcpus=4, memory_mb=4096, disk_size_gb=50)
            )
        assert "bigvm" in result
        assert "4096 MB" in result

    async def test_with_boot_iso(self):
        dom = make_mock_domain(name="installvm")
        self.conn.defineXML.return_value = dom
        with patch("server._provision_disk", new_callable=AsyncMock):
            with patch("server._build_domain_xml", wraps=server._build_domain_xml) as mock_xml:
                result = await libvirt_create_vm(
                    CreateVMInput(alias="lab", name="installvm", boot_iso="/images/install.iso")
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
        with patch("server._provision_disk", new_callable=AsyncMock, side_effect=RuntimeError("disk error")):
            result = await libvirt_create_vm(
                CreateVMInput(alias="lab", name="newvm")
            )
        assert "Error" in result

    async def test_define_xml_fails(self):
        self.conn.defineXML.side_effect = libvirt.libvirtError("bad XML")
        with patch("server._provision_disk", new_callable=AsyncMock):
            result = await libvirt_create_vm(
                CreateVMInput(alias="lab", name="newvm")
            )
        assert "Error" in result
