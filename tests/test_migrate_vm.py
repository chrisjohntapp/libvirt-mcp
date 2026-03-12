"""Unit tests for VM migration: _rewrite_disk_paths, _scp_between_hosts, libvirt_migrate_vm."""

import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, MagicMock, patch

import libvirt
import pytest

import server
from server import (
    MigrateVMInput,
    MigrationStatusInput,
    _rewrite_disk_paths,
    _scp_between_hosts,
    _migrate_vm_offline,
    libvirt_get_migration_status,
    libvirt_migrate_vm,
)


@pytest.fixture(autouse=True)
def clear_connections():
    server._connections.clear()
    yield
    server._connections.clear()


def make_mock_conn(uri="qemu+ssh://root@srchost:22/system"):
    conn = MagicMock(spec=libvirt.virConnect)
    conn.getVersion.return_value = 9007001
    conn.getHostname.return_value = "testhost"
    conn.getLibVersion.return_value = 10002000
    conn.getURI.return_value = uri
    return conn


def make_mock_domain(
    name="test-vm",
    uuid="aaaaaaaa-0000-0000-0000-000000000001",
    state=libvirt.VIR_DOMAIN_RUNNING,
):
    dom = MagicMock(spec=libvirt.virDomain)
    dom.name.return_value = name
    dom.UUIDString.return_value = uuid
    dom.info.return_value = [state, 2097152, 1048576, 2, 0]
    dom.isPersistent.return_value = 1
    dom.autostart.return_value = 0
    dom.XMLDesc.return_value = (
        f"<domain type='kvm'><name>{name}</name>"
        f"<uuid>{uuid}</uuid>"
        f"<devices><disk type='file' device='disk'>"
        f"<source file='/var/lib/libvirt/images/{name}.qcow2'/>"
        f"<target dev='vda' bus='virtio'/></disk></devices></domain>"
    )
    return dom


SAMPLE_XML = (
    "<domain type='kvm'><name>vm1</name>"
    "<uuid>aaaaaaaa-0000-0000-0000-000000000001</uuid>"
    "<devices><disk type='file' device='disk'>"
    "<source file='/var/lib/libvirt/images/vm1.qcow2'/>"
    "<target dev='vda' bus='virtio'/></disk></devices></domain>"
)


class TestRewriteDiskPaths:
    def test_strips_uuid(self):
        result = _rewrite_disk_paths(SAMPLE_XML, {})
        root = ET.fromstring(result)
        assert root.find("uuid") is None

    def test_path_rewrite(self):
        path_map = {"/var/lib/libvirt/images/vm1.qcow2": "/data/images/vm1.qcow2"}
        result = _rewrite_disk_paths(SAMPLE_XML, path_map)
        root = ET.fromstring(result)
        source = root.find(".//disk[@device='disk']/source")
        assert source.get("file") == "/data/images/vm1.qcow2"

    def test_no_disks(self):
        xml = "<domain type='kvm'><name>vm1</name><uuid>abc</uuid><devices></devices></domain>"
        result = _rewrite_disk_paths(xml, {})
        root = ET.fromstring(result)
        assert root.find("uuid") is None
        assert root.find("name").text == "vm1"


class TestScpBetweenHosts:
    @patch("server._ssh_run", new_callable=AsyncMock)
    async def test_direct_scp_success(self, mock_ssh):
        await _scp_between_hosts(
            "src",
            "root",
            22,
            None,
            "dst",
            "root",
            22,
            None,
            "/images/vm.qcow2",
            "/images/vm.qcow2",
        )
        mock_ssh.assert_called_once()
        cmd = mock_ssh.call_args[0][4]
        assert "scp" in cmd
        assert "dst" in cmd

    @patch("asyncio.create_subprocess_shell")
    @patch(
        "server._ssh_run",
        new_callable=AsyncMock,
        side_effect=RuntimeError("direct failed"),
    )
    async def test_direct_fails_fallback_succeeds(self, mock_ssh, mock_shell):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        mock_shell.return_value = mock_proc

        await _scp_between_hosts(
            "src",
            "root",
            22,
            None,
            "dst",
            "root",
            22,
            None,
            "/images/vm.qcow2",
            "/images/vm.qcow2",
        )
        mock_shell.assert_called_once()
        cmd = mock_shell.call_args[0][0]
        assert "sudo cat" in cmd
        assert "sudo tee" in cmd

    @patch("asyncio.create_subprocess_shell")
    @patch(
        "server._ssh_run",
        new_callable=AsyncMock,
        side_effect=RuntimeError("direct failed"),
    )
    async def test_both_fail(self, mock_ssh, mock_shell):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"relay error")
        mock_proc.returncode = 1
        mock_shell.return_value = mock_proc

        with pytest.raises(RuntimeError, match="Relay transfer failed"):
            await _scp_between_hosts(
                "src",
                "root",
                22,
                None,
                "dst",
                "root",
                22,
                None,
                "/images/vm.qcow2",
                "/images/vm.qcow2",
            )


class TestMigrateVM:
    def setup_method(self):
        self.src_conn = make_mock_conn("qemu+ssh://root@srchost:22/system")
        self.tgt_conn = make_mock_conn("qemu+ssh://root@dsthost:22/system")
        server._connections["source"] = self.src_conn
        server._connections["target"] = self.tgt_conn

    @patch("server._run_migration_job", new_callable=AsyncMock)
    @patch("asyncio.create_task")
    async def test_starts_async_job(self, mock_create_task, mock_run_job):
        dom = make_mock_domain(name="migrate-me", state=libvirt.VIR_DOMAIN_SHUTOFF)
        self.src_conn.lookupByName.return_value = dom
        self.tgt_conn.lookupByName.side_effect = libvirt.libvirtError("not found")

        result = await libvirt_migrate_vm(
            MigrateVMInput(
                source_alias="source", target_alias="target", domain="migrate-me"
            )
        )
        assert "Migration started" in result
        assert "Job ID:" in result
        mock_create_task.assert_called_once()
        mock_run_job.assert_called_once()
        scheduled = mock_create_task.call_args[0][0]
        scheduled.close()

    @patch("server._scp_between_hosts", new_callable=AsyncMock)
    async def test_offline_migrate_happy_path(self, mock_scp):
        dom = make_mock_domain(name="migrate-me", state=libvirt.VIR_DOMAIN_RUNNING)
        dom.info.side_effect = [
            [libvirt.VIR_DOMAIN_RUNNING, 2097152, 1048576, 2, 0],
            [libvirt.VIR_DOMAIN_SHUTOFF, 2097152, 1048576, 2, 0],
        ]
        self.src_conn.lookupByName.return_value = dom
        self.tgt_conn.lookupByName.side_effect = libvirt.libvirtError("not found")

        new_dom = make_mock_domain(
            name="migrate-me", uuid="bbbbbbbb-0000-0000-0000-000000000002"
        )
        self.tgt_conn.defineXML.return_value = new_dom

        result = await _migrate_vm_offline(
            MigrateVMInput(
                source_alias="source", target_alias="target", domain="migrate-me"
            )
        )
        assert "migrated" in result
        assert "target" in result
        dom.shutdown.assert_called_once()
        dom.destroy.assert_not_called()
        assert dom.XMLDesc.call_count == 2  # inactive XML + _get_domain_disks
        mock_scp.assert_called_once()
        self.tgt_conn.defineXML.assert_called_once()
        new_dom.create.assert_called_once()

    async def test_source_not_connected(self):
        server._connections.pop("source")
        result = await libvirt_migrate_vm(
            MigrateVMInput(source_alias="source", target_alias="target", domain="vm1")
        )
        assert "Error" in result
        assert "No connection" in result

    async def test_target_not_connected(self):
        server._connections.pop("target")
        result = await libvirt_migrate_vm(
            MigrateVMInput(source_alias="source", target_alias="target", domain="vm1")
        )
        assert "Error" in result
        assert "No connection" in result

    async def test_domain_not_found(self):
        self.src_conn.lookupByName.side_effect = libvirt.libvirtError("not found")
        self.src_conn.lookupByUUIDString.side_effect = libvirt.libvirtError("not found")
        result = await libvirt_migrate_vm(
            MigrateVMInput(source_alias="source", target_alias="target", domain="ghost")
        )
        assert "Error" in result

    @patch("server._scp_between_hosts", new_callable=AsyncMock)
    async def test_domain_exists_on_target(self, mock_scp):
        dom = make_mock_domain(name="dupe-vm")
        self.src_conn.lookupByName.return_value = dom
        self.tgt_conn.lookupByName.return_value = dom  # Already exists

        result = await _migrate_vm_offline(
            MigrateVMInput(
                source_alias="source", target_alias="target", domain="dupe-vm"
            )
        )
        assert "already exists" in result
        mock_scp.assert_not_called()

    @patch("server._scp_between_hosts", new_callable=AsyncMock)
    async def test_retry_after_timeout_detects_already_completed(self, mock_scp):
        src_dom = make_mock_domain(name="dupe-vm", state=libvirt.VIR_DOMAIN_SHUTOFF)
        tgt_dom = make_mock_domain(name="dupe-vm", state=libvirt.VIR_DOMAIN_RUNNING)
        self.src_conn.lookupByName.return_value = src_dom
        self.tgt_conn.lookupByName.return_value = tgt_dom

        result = await _migrate_vm_offline(
            MigrateVMInput(
                source_alias="source", target_alias="target", domain="dupe-vm"
            )
        )
        assert "already completed" in result
        assert "cleanup" in result.lower()
        mock_scp.assert_not_called()

    @patch("server._scp_between_hosts", new_callable=AsyncMock)
    async def test_running_vm_stopped_first(self, mock_scp):
        dom = make_mock_domain(name="vm1", state=libvirt.VIR_DOMAIN_RUNNING)
        dom.info.side_effect = [
            [libvirt.VIR_DOMAIN_RUNNING, 2097152, 1048576, 2, 0],
            [libvirt.VIR_DOMAIN_SHUTOFF, 2097152, 1048576, 2, 0],
        ]
        self.src_conn.lookupByName.return_value = dom
        self.tgt_conn.lookupByName.side_effect = libvirt.libvirtError("not found")
        new_dom = make_mock_domain(name="vm1")
        self.tgt_conn.defineXML.return_value = new_dom

        await _migrate_vm_offline(
            MigrateVMInput(source_alias="source", target_alias="target", domain="vm1")
        )
        dom.shutdown.assert_called_once()
        dom.destroy.assert_not_called()

    @patch("server._scp_between_hosts", new_callable=AsyncMock)
    async def test_shutdown_timeout_falls_back_to_destroy(self, mock_scp):
        dom = make_mock_domain(name="vm1", state=libvirt.VIR_DOMAIN_RUNNING)
        self.src_conn.lookupByName.return_value = dom
        self.tgt_conn.lookupByName.side_effect = libvirt.libvirtError("not found")
        new_dom = make_mock_domain(name="vm1")
        self.tgt_conn.defineXML.return_value = new_dom

        await _migrate_vm_offline(
            MigrateVMInput(
                source_alias="source",
                target_alias="target",
                domain="vm1",
                shutdown_timeout_seconds=1,
            )
        )
        dom.shutdown.assert_called_once()
        dom.destroy.assert_called_once()

    @patch("server._scp_between_hosts", new_callable=AsyncMock)
    async def test_shutdown_error_falls_back_to_destroy(self, mock_scp):
        dom = make_mock_domain(name="vm1", state=libvirt.VIR_DOMAIN_RUNNING)
        dom.shutdown.side_effect = libvirt.libvirtError("shutdown failed")
        self.src_conn.lookupByName.return_value = dom
        self.tgt_conn.lookupByName.side_effect = libvirt.libvirtError("not found")
        new_dom = make_mock_domain(name="vm1")
        self.tgt_conn.defineXML.return_value = new_dom

        await _migrate_vm_offline(
            MigrateVMInput(source_alias="source", target_alias="target", domain="vm1")
        )
        dom.shutdown.assert_called_once()
        dom.destroy.assert_called_once()

    @patch("server._scp_between_hosts", new_callable=AsyncMock)
    async def test_already_shutoff(self, mock_scp):
        dom = make_mock_domain(name="vm1", state=libvirt.VIR_DOMAIN_SHUTOFF)
        self.src_conn.lookupByName.return_value = dom
        self.tgt_conn.lookupByName.side_effect = libvirt.libvirtError("not found")
        new_dom = make_mock_domain(name="vm1")
        self.tgt_conn.defineXML.return_value = new_dom

        await _migrate_vm_offline(
            MigrateVMInput(source_alias="source", target_alias="target", domain="vm1")
        )
        dom.destroy.assert_not_called()

    async def test_disk_transfer_failure(self):
        dom = make_mock_domain(name="vm1", state=libvirt.VIR_DOMAIN_SHUTOFF)
        self.src_conn.lookupByName.return_value = dom
        self.tgt_conn.lookupByName.side_effect = libvirt.libvirtError("not found")

        with patch(
            "server._scp_between_hosts",
            new_callable=AsyncMock,
            side_effect=RuntimeError("scp failed"),
        ):
            with pytest.raises(RuntimeError, match="scp failed"):
                await _migrate_vm_offline(
                    MigrateVMInput(
                        source_alias="source", target_alias="target", domain="vm1"
                    )
                )
        self.tgt_conn.defineXML.assert_not_called()


class TestMigrationStatus:
    def setup_method(self):
        server._migration_jobs.clear()

    async def test_status_not_found(self):
        result = await libvirt_get_migration_status(
            MigrationStatusInput(job_id="missing")
        )
        assert "not found" in result.lower()

    async def test_status_success_includes_timeline(self):
        server._migration_jobs["job-1"] = {
            "job_id": "job-1",
            "status": "succeeded",
            "source_alias": "source",
            "target_alias": "target",
            "domain": "vm1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "started_at": "2026-01-01T00:00:01+00:00",
            "finished_at": "2026-01-01T00:00:10+00:00",
            "phase": "done",
            "phases": [
                {"phase": "queued", "at": "2026-01-01T00:00:00+00:00"},
                {"phase": "done", "at": "2026-01-01T00:00:10+00:00"},
            ],
            "result": "ok",
            "error": None,
        }
        result = await libvirt_get_migration_status(
            MigrationStatusInput(job_id="job-1")
        )
        assert "Migration Job job-1" in result
        assert "status: succeeded" in result
        assert "Phase Timeline" in result


class TestMigrateVMCleanup:
    def setup_method(self):
        self.src_conn = make_mock_conn("qemu+ssh://root@srchost:22/system")
        self.tgt_conn = make_mock_conn("qemu+ssh://root@dsthost:22/system")
        server._connections["source"] = self.src_conn
        server._connections["target"] = self.tgt_conn

    @patch("server._ssh_run", new_callable=AsyncMock)
    async def test_cleanup_success(self, mock_ssh):
        dom = make_mock_domain(name="vm1", state=libvirt.VIR_DOMAIN_SHUTOFF)
        self.src_conn.lookupByName.return_value = dom

        result = await libvirt_migrate_vm(
            MigrateVMInput(
                source_alias="source", target_alias="target", domain="vm1", confirm=True
            )
        )
        assert "cleanup complete" in result.lower()
        dom.undefine.assert_called_once()
        mock_ssh.assert_called_once()
        assert "rm -f" in mock_ssh.call_args[0][4]

    async def test_cleanup_domain_still_running(self):
        dom = make_mock_domain(name="vm1", state=libvirt.VIR_DOMAIN_RUNNING)
        self.src_conn.lookupByName.return_value = dom

        result = await libvirt_migrate_vm(
            MigrateVMInput(
                source_alias="source", target_alias="target", domain="vm1", confirm=True
            )
        )
        assert "still running" in result
        dom.undefine.assert_not_called()
