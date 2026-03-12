"""
Unit tests for server.py.

All libvirt interactions are mocked -- no real libvirt connection is required.
"""

import json
import pytest
from unittest.mock import MagicMock, patch
import libvirt

import server
from unittest.mock import AsyncMock
from server import (
    _domain_state_str,
    _format_error,
    _domain_summary,
    _get_conn,
    _get_domain_disks,
    _lookup_domain,
    libvirt_connect_host,
    libvirt_disconnect_host,
    libvirt_list_hosts,
    libvirt_list_domains,
    libvirt_get_domain_info,
    libvirt_get_domain_xml,
    libvirt_start_domain,
    libvirt_shutdown_domain,
    libvirt_destroy_domain,
    libvirt_reboot_domain,
    libvirt_suspend_domain,
    libvirt_resume_domain,
    libvirt_define_domain,
    libvirt_undefine_domain,
    libvirt_delete_vm,
    ConnectHostInput,
    HostInput,
    DomainInput,
    DeleteVmInput,
    ListDomainsInput,
    DefineVMInput,
    DomainInfoInput,
    ResponseFormat,
)


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
    return conn


def make_mock_domain(
    name="test-vm",
    uuid="aaaaaaaa-0000-0000-0000-000000000001",
    state=libvirt.VIR_DOMAIN_RUNNING,
    max_mem=2097152,
    mem=1048576,
    vcpus=2,
    persistent=True,
    autostart=False,
    autostart_raises=False,
):
    dom = MagicMock(spec=libvirt.virDomain)
    dom.name.return_value = name
    dom.UUIDString.return_value = uuid
    dom.info.return_value = [state, max_mem, mem, vcpus, 0]
    dom.isPersistent.return_value = int(persistent)
    if autostart_raises:
        dom.autostart.side_effect = libvirt.libvirtError("not supported")
    else:
        dom.autostart.return_value = int(autostart)
    dom.XMLDesc.return_value = (
        f"<domain type='kvm'><name>{name}</name>"
        f"<devices><disk type='file' device='disk'>"
        f"<source file='/var/lib/libvirt/images/{name}.qcow2'/>"
        f"<target dev='vda' bus='virtio'/></disk></devices></domain>"
    )
    return dom


class TestDomainStateStr:
    def test_known_states(self):
        assert _domain_state_str(libvirt.VIR_DOMAIN_NOSTATE) == "no state"
        assert _domain_state_str(libvirt.VIR_DOMAIN_RUNNING) == "running"
        assert _domain_state_str(libvirt.VIR_DOMAIN_BLOCKED) == "blocked"
        assert _domain_state_str(libvirt.VIR_DOMAIN_PAUSED) == "paused"
        assert _domain_state_str(libvirt.VIR_DOMAIN_SHUTDOWN) == "shutting down"
        assert _domain_state_str(libvirt.VIR_DOMAIN_SHUTOFF) == "shutoff"
        assert _domain_state_str(libvirt.VIR_DOMAIN_CRASHED) == "crashed"
        assert _domain_state_str(libvirt.VIR_DOMAIN_PMSUSPENDED) == "suspended (PM)"

    def test_unknown_state(self):
        result = _domain_state_str(999)
        assert "unknown" in result
        assert "999" in result


class TestFormatError:
    def test_libvirt_error(self):
        err = libvirt.libvirtError("some libvirt problem")
        result = _format_error(err, "testing")
        assert "Error (testing):" in result

    def test_value_error(self):
        err = ValueError("bad value")
        result = _format_error(err, "ctx")
        assert "Error (ctx): bad value" == result

    def test_unexpected_error(self):
        err = RuntimeError("unexpected")
        result = _format_error(err)
        assert "RuntimeError" in result

    def test_no_context(self):
        err = ValueError("oops")
        result = _format_error(err)
        assert result == "Error: oops"


class TestDomainSummary:
    def test_persistent_domain(self):
        dom = make_mock_domain(persistent=True, autostart=True)
        s = _domain_summary(dom)
        assert s["name"] == "test-vm"
        assert s["state"] == "running"
        assert s["vcpus"] == 2
        assert s["persistent"] is True
        assert s["autostart"] is True
        assert s["max_memory_mb"] == 2048
        assert s["current_memory_mb"] == 1024

    def test_transient_domain_autostart_none(self):
        dom = make_mock_domain(persistent=False, autostart_raises=True)
        s = _domain_summary(dom)
        assert s["autostart"] is None
        assert s["persistent"] is False


class TestGetConn:
    def test_missing_alias(self):
        with pytest.raises(ValueError, match="No connection found"):
            _get_conn("nonexistent")

    def test_dropped_connection(self):
        conn = make_mock_conn()
        conn.getVersion.side_effect = libvirt.libvirtError("connection closed")
        server._connections["myhost"] = conn
        with pytest.raises(ValueError, match="has dropped"):
            _get_conn("myhost")
        assert "myhost" not in server._connections

    def test_live_connection(self):
        conn = make_mock_conn()
        server._connections["myhost"] = conn
        result = _get_conn("myhost")
        assert result is conn


class TestLookupDomain:
    def test_by_name(self):
        conn = make_mock_conn()
        dom = make_mock_domain()
        conn.lookupByName.return_value = dom
        result = _lookup_domain(conn, "test-vm")
        assert result is dom

    def test_by_uuid(self):
        conn = make_mock_conn()
        dom = make_mock_domain()
        conn.lookupByName.side_effect = libvirt.libvirtError("not found")
        conn.lookupByUUIDString.return_value = dom
        result = _lookup_domain(conn, "aaaaaaaa-0000-0000-0000-000000000001")
        assert result is dom

    def test_not_found(self):
        conn = make_mock_conn()
        conn.lookupByName.side_effect = libvirt.libvirtError("not found")
        conn.lookupByUUIDString.side_effect = libvirt.libvirtError("not found")
        with pytest.raises(ValueError, match="not found"):
            _lookup_domain(conn, "ghost-vm")


class TestConnectHost:
    async def test_connect_success(self):
        conn = make_mock_conn()
        with patch("libvirt.open", return_value=conn):
            result = await libvirt_connect_host(
                ConnectHostInput(host="192.168.1.1", alias="lab")
            )
        assert "Connected to 'lab'" in result
        assert "testhost" in result
        assert "lab" in server._connections

    async def test_connect_replaces_existing(self):
        old_conn = make_mock_conn()
        server._connections["lab"] = old_conn
        new_conn = make_mock_conn()
        with patch("libvirt.open", return_value=new_conn):
            await libvirt_connect_host(ConnectHostInput(host="192.168.1.1", alias="lab"))
        old_conn.close.assert_called_once()
        assert server._connections["lab"] is new_conn

    async def test_connect_open_returns_none(self):
        with patch("libvirt.open", return_value=None):
            result = await libvirt_connect_host(
                ConnectHostInput(host="192.168.1.1", alias="lab")
            )
        assert "Error" in result
        assert "None" in result

    async def test_connect_libvirt_error(self):
        with patch("libvirt.open", side_effect=libvirt.libvirtError("auth failed")):
            result = await libvirt_connect_host(
                ConnectHostInput(host="192.168.1.1", alias="lab")
            )
        assert "Error" in result

    async def test_connect_with_ssh_key(self):
        conn = make_mock_conn()
        with patch("libvirt.open", return_value=conn) as mock_open:
            await libvirt_connect_host(
                ConnectHostInput(host="myhost", alias="lab", ssh_key_path="/home/user/.ssh/id_rsa")
            )
        uri = mock_open.call_args[0][0]
        assert "keyfile=" in uri

    async def test_connect_with_user_and_port(self):
        conn = make_mock_conn()
        with patch("libvirt.open", return_value=conn) as mock_open:
            await libvirt_connect_host(
                ConnectHostInput(host="myhost", alias="lab", user="ubuntu", port=2222)
            )
        uri = mock_open.call_args[0][0]
        assert "ubuntu@" in uri
        assert ":2222" in uri


class TestDisconnectHost:
    async def test_disconnect_success(self):
        conn = make_mock_conn()
        server._connections["lab"] = conn
        result = await libvirt_disconnect_host(HostInput(alias="lab"))
        assert "Disconnected from 'lab'" in result
        assert "lab" not in server._connections
        conn.close.assert_called_once()

    async def test_disconnect_not_found(self):
        result = await libvirt_disconnect_host(HostInput(alias="ghost"))
        assert "No active connection" in result


class TestListHosts:
    async def test_empty(self):
        result = await libvirt_list_hosts()
        assert "No hosts connected" in result

    async def test_live_host(self):
        conn = make_mock_conn()
        server._connections["lab"] = conn
        result = await libvirt_list_hosts()
        assert "lab" in result
        assert "testhost" in result
        assert "live" in result

    async def test_dropped_host(self):
        conn = make_mock_conn()
        conn.getHostname.side_effect = Exception("connection lost")
        server._connections["lab"] = conn
        result = await libvirt_list_hosts()
        assert "dropped" in result


class TestListDomains:
    def setup_method(self):
        self.conn = make_mock_conn()
        server._connections["lab"] = self.conn

    async def test_list_all(self):
        domains = [make_mock_domain("vm1"), make_mock_domain("vm2", state=libvirt.VIR_DOMAIN_SHUTOFF)]
        self.conn.listAllDomains.return_value = domains
        result = await libvirt_list_domains(ListDomainsInput(alias="lab"))
        assert "vm1" in result
        assert "vm2" in result

    async def test_filter_running(self):
        domains = [
            make_mock_domain("running-vm", state=libvirt.VIR_DOMAIN_RUNNING),
            make_mock_domain("stopped-vm", state=libvirt.VIR_DOMAIN_SHUTOFF),
        ]
        self.conn.listAllDomains.return_value = domains
        result = await libvirt_list_domains(ListDomainsInput(alias="lab", state_filter="running"))
        assert "running-vm" in result
        assert "stopped-vm" not in result

    async def test_invalid_filter(self):
        self.conn.listAllDomains.return_value = []
        result = await libvirt_list_domains(ListDomainsInput(alias="lab", state_filter="stopped"))
        assert "Invalid state_filter" in result
        assert "stopped" in result

    async def test_empty_result(self):
        self.conn.listAllDomains.return_value = []
        result = await libvirt_list_domains(ListDomainsInput(alias="lab"))
        assert "No domains found" in result

    async def test_json_format(self):
        domains = [make_mock_domain("vm1")]
        self.conn.listAllDomains.return_value = domains
        result = await libvirt_list_domains(
            ListDomainsInput(alias="lab", response_format=ResponseFormat.JSON)
        )
        data = json.loads(result)
        assert data["count"] == 1
        assert data["domains"][0]["name"] == "vm1"

    async def test_missing_connection(self):
        result = await libvirt_list_domains(ListDomainsInput(alias="nonexistent"))
        assert "Error" in result

    async def test_sorted_by_name(self):
        domains = [make_mock_domain("z-vm"), make_mock_domain("a-vm")]
        self.conn.listAllDomains.return_value = domains
        result = await libvirt_list_domains(ListDomainsInput(alias="lab"))
        assert result.index("a-vm") < result.index("z-vm")


class TestGetDomainInfo:
    def setup_method(self):
        self.conn = make_mock_conn()
        server._connections["lab"] = self.conn

    async def test_markdown_format(self):
        dom = make_mock_domain()
        self.conn.lookupByName.return_value = dom
        result = await libvirt_get_domain_info(DomainInfoInput(alias="lab", domain="test-vm"))
        assert "# Domain: test-vm" in result
        assert "running" in result
        assert "UUID" in result

    async def test_json_format(self):
        dom = make_mock_domain()
        self.conn.lookupByName.return_value = dom
        result = await libvirt_get_domain_info(
            DomainInfoInput(alias="lab", domain="test-vm", response_format=ResponseFormat.JSON)
        )
        data = json.loads(result)
        assert data["name"] == "test-vm"
        assert data["state"] == "running"

    async def test_transient_autostart_display(self):
        dom = make_mock_domain(autostart_raises=True, persistent=False)
        self.conn.lookupByName.return_value = dom
        result = await libvirt_get_domain_info(DomainInfoInput(alias="lab", domain="test-vm"))
        assert "n/a (transient)" in result

    async def test_domain_not_found(self):
        self.conn.lookupByName.side_effect = libvirt.libvirtError("not found")
        self.conn.lookupByUUIDString.side_effect = libvirt.libvirtError("not found")
        result = await libvirt_get_domain_info(DomainInfoInput(alias="lab", domain="ghost"))
        assert "Error" in result


class TestGetDomainXml:
    async def test_returns_xml(self):
        conn = make_mock_conn()
        server._connections["lab"] = conn
        dom = make_mock_domain()
        conn.lookupByName.return_value = dom
        result = await libvirt_get_domain_xml(DomainInput(alias="lab", domain="test-vm"))
        assert result.startswith("<domain")
        dom.XMLDesc.assert_called_once_with(0)


class TestDomainLifecycle:
    def setup_method(self):
        self.conn = make_mock_conn()
        server._connections["lab"] = self.conn
        self.dom = make_mock_domain()
        self.conn.lookupByName.return_value = self.dom

    async def test_start_success(self):
        result = await libvirt_start_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "started" in result
        self.dom.create.assert_called_once()

    async def test_start_error(self):
        self.dom.create.side_effect = libvirt.libvirtError("already running")
        result = await libvirt_start_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Error" in result

    async def test_shutdown_success(self):
        result = await libvirt_shutdown_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Shutdown signal sent" in result
        self.dom.shutdown.assert_called_once()

    async def test_shutdown_error(self):
        self.dom.shutdown.side_effect = libvirt.libvirtError("not running")
        result = await libvirt_shutdown_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Error" in result

    async def test_destroy_success(self):
        result = await libvirt_destroy_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "force-stopped" in result
        self.dom.destroy.assert_called_once()

    async def test_destroy_error(self):
        self.dom.destroy.side_effect = libvirt.libvirtError("not running")
        result = await libvirt_destroy_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Error" in result

    async def test_reboot_success(self):
        result = await libvirt_reboot_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Reboot signal sent" in result
        self.dom.reboot.assert_called_once_with(0)

    async def test_reboot_error(self):
        self.dom.reboot.side_effect = libvirt.libvirtError("not running")
        result = await libvirt_reboot_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Error" in result

    async def test_suspend_success(self):
        result = await libvirt_suspend_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "suspended" in result
        self.dom.suspend.assert_called_once()

    async def test_suspend_error(self):
        self.dom.suspend.side_effect = libvirt.libvirtError("not running")
        result = await libvirt_suspend_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Error" in result

    async def test_resume_success(self):
        result = await libvirt_resume_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "resumed" in result
        self.dom.resume.assert_called_once()

    async def test_resume_error(self):
        self.dom.resume.side_effect = libvirt.libvirtError("not running")
        result = await libvirt_resume_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Error" in result


class TestDefineDomain:
    async def test_define_success(self):
        conn = make_mock_conn()
        server._connections["lab"] = conn
        dom = make_mock_domain(name="new-vm")
        conn.defineXML.return_value = dom
        result = await libvirt_define_domain(
            DefineVMInput(alias="lab", xml="<domain type='kvm'><name>new-vm</name></domain>")
        )
        assert "new-vm" in result
        assert "defined" in result

    async def test_define_error(self):
        conn = make_mock_conn()
        server._connections["lab"] = conn
        conn.defineXML.side_effect = libvirt.libvirtError("invalid XML")
        result = await libvirt_define_domain(
            DefineVMInput(alias="lab", xml="<domain type='kvm'><name>x</name></domain>")
        )
        assert "Error" in result


class TestUndefineDomain:
    async def test_undefine_success(self):
        conn = make_mock_conn()
        server._connections["lab"] = conn
        dom = make_mock_domain(name="old-vm")
        conn.lookupByName.return_value = dom
        result = await libvirt_undefine_domain(DomainInput(alias="lab", domain="old-vm"))
        assert "old-vm" in result
        assert "undefined" in result
        assert "Disk images were NOT deleted" in result
        dom.undefine.assert_called_once()

    async def test_undefine_error(self):
        conn = make_mock_conn()
        server._connections["lab"] = conn
        dom = make_mock_domain()
        conn.lookupByName.return_value = dom
        dom.undefine.side_effect = libvirt.libvirtError("domain is running")
        result = await libvirt_undefine_domain(DomainInput(alias="lab", domain="test-vm"))
        assert "Error" in result


class TestGetDomainDisks:
    def test_extracts_disk_paths(self):
        dom = make_mock_domain(name="myvm")
        disks = _get_domain_disks(dom)
        assert disks == ["/var/lib/libvirt/images/myvm.qcow2"]

    def test_no_disks(self):
        dom = make_mock_domain()
        dom.XMLDesc.return_value = "<domain type='kvm'><name>test</name><devices></devices></domain>"
        assert _get_domain_disks(dom) == []


@pytest.mark.asyncio
class TestDeleteVm:
    async def test_dry_run(self):
        conn = make_mock_conn()
        server._connections["lab"] = conn
        dom = make_mock_domain(name="del-me")
        conn.lookupByName.return_value = dom
        result = await libvirt_delete_vm(DeleteVmInput(alias="lab", domain="del-me", confirm=False))
        assert "DELETE PREVIEW" in result
        assert "del-me" in result
        assert "/var/lib/libvirt/images/del-me.qcow2" in result
        assert "confirm=true" in result
        dom.destroy.assert_not_called()
        dom.undefine.assert_not_called()

    @patch("server._ssh_run", new_callable=AsyncMock)
    async def test_confirmed_running_vm(self, mock_ssh):
        conn = make_mock_conn()
        conn.getURI.return_value = "qemu+ssh://user@host/system"
        server._connections["lab"] = conn
        dom = make_mock_domain(name="del-me", state=libvirt.VIR_DOMAIN_RUNNING)
        conn.lookupByName.return_value = dom
        result = await libvirt_delete_vm(DeleteVmInput(alias="lab", domain="del-me", confirm=True))
        assert "deleted" in result
        assert "del-me" in result
        dom.destroy.assert_called_once()
        dom.undefine.assert_called_once()
        mock_ssh.assert_called_once()
        assert "/var/lib/libvirt/images/del-me.qcow2" in mock_ssh.call_args[0][4]

    @patch("server._ssh_run", new_callable=AsyncMock)
    async def test_confirmed_shutoff_vm(self, mock_ssh):
        conn = make_mock_conn()
        conn.getURI.return_value = "qemu+ssh://user@host/system"
        server._connections["lab"] = conn
        dom = make_mock_domain(name="del-me", state=libvirt.VIR_DOMAIN_SHUTOFF)
        conn.lookupByName.return_value = dom
        result = await libvirt_delete_vm(DeleteVmInput(alias="lab", domain="del-me", confirm=True))
        assert "deleted" in result
        dom.destroy.assert_not_called()
        dom.undefine.assert_called_once()

    @patch("server._ssh_run", new_callable=AsyncMock)
    async def test_no_disks(self, mock_ssh):
        conn = make_mock_conn()
        conn.getURI.return_value = "qemu+ssh://user@host/system"
        server._connections["lab"] = conn
        dom = make_mock_domain(name="nodisk", state=libvirt.VIR_DOMAIN_SHUTOFF)
        dom.XMLDesc.return_value = "<domain type='kvm'><name>nodisk</name><devices></devices></domain>"
        conn.lookupByName.return_value = dom
        result = await libvirt_delete_vm(DeleteVmInput(alias="lab", domain="nodisk", confirm=True))
        assert "No disk files" in result
        mock_ssh.assert_not_called()

    @patch("server._ssh_run", new_callable=AsyncMock)
    async def test_disk_delete_error(self, mock_ssh):
        mock_ssh.side_effect = RuntimeError("SSH failed")
        conn = make_mock_conn()
        conn.getURI.return_value = "qemu+ssh://user@host/system"
        server._connections["lab"] = conn
        dom = make_mock_domain(name="del-me", state=libvirt.VIR_DOMAIN_SHUTOFF)
        conn.lookupByName.return_value = dom
        result = await libvirt_delete_vm(DeleteVmInput(alias="lab", domain="del-me", confirm=True))
        assert "deleted" in result
        assert "errors" in result.lower()
        dom.undefine.assert_called_once()
