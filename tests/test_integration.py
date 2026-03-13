"""
Integration tests for server.py against a real libvirt host.

Set LIBVIRT_TEST_HOST to run these tests. The value supports one or more hosts
as a comma-separated list (for example, to support migration tests).

Examples:
    LIBVIRT_TEST_HOST=host-a.example.com pytest tests/test_integration.py -v
    LIBVIRT_TEST_HOST=host-a.example.com,host-b.example.com pytest tests/test_integration.py -v
"""

import os
import json
import asyncio
from uuid import uuid4
import pytest

import server
from server import (
    libvirt_connect_host,
    libvirt_disconnect_host,
    libvirt_list_hosts,
    libvirt_list_domains,
    libvirt_get_domain_info,
    libvirt_get_domain_xml,
    libvirt_create_vm,
    libvirt_delete_vm,
    libvirt_migrate_vm,
    libvirt_get_migration_status,
    libvirt_list_templates,
    ConnectHostInput,
    HostInput,
    DomainInput,
    DeleteVmInput,
    ListDomainsInput,
    DomainInfoInput,
    CreateVMInput,
    MigrateVMInput,
    MigrationStatusInput,
    ResponseFormat,
)

INTEGRATION_HOSTS_RAW = os.environ.get("LIBVIRT_TEST_HOST", "")
INTEGRATION_HOSTS = [h.strip() for h in INTEGRATION_HOSTS_RAW.split(",") if h.strip()]
INTEGRATION_HOST = INTEGRATION_HOSTS[0] if INTEGRATION_HOSTS else ""
ALIAS = "integration-test"
MIGRATION_SOURCE_ALIAS = "integration-src"
MIGRATION_TARGET_ALIAS = "integration-dst"
MIGRATION_POLL_INTERVAL_SECONDS = 2
MIGRATION_TIMEOUT_SECONDS = 900

pytestmark = pytest.mark.skipif(
    not INTEGRATION_HOST,
    reason="Set LIBVIRT_TEST_HOST to run integration tests",
)

requires_two_hosts = pytest.mark.skipif(
    len(INTEGRATION_HOSTS) < 2,
    reason="Migration integration tests require at least two hosts in LIBVIRT_TEST_HOST",
)


@pytest.fixture(autouse=True)
def clear_connections():
    server._connections.clear()
    yield
    server._connections.clear()


@pytest.fixture
async def connected():
    """Establish a real connection before each test and clean up after."""
    result = await libvirt_connect_host(
        ConnectHostInput(host=INTEGRATION_HOST, alias=ALIAS)
    )
    assert "Error" not in result, f"Connection failed: {result}"
    yield
    await libvirt_disconnect_host(HostInput(alias=ALIAS))


@pytest.fixture
async def connected_source_target():
    """Connect to two hosts for migration tests and clean up both aliases."""
    source_host = INTEGRATION_HOSTS[0]
    target_host = INTEGRATION_HOSTS[1]
    src_result = await libvirt_connect_host(
        ConnectHostInput(host=source_host, alias=MIGRATION_SOURCE_ALIAS)
    )
    assert "Error" not in src_result, f"Source connection failed: {src_result}"
    tgt_result = await libvirt_connect_host(
        ConnectHostInput(host=target_host, alias=MIGRATION_TARGET_ALIAS)
    )
    assert "Error" not in tgt_result, f"Target connection failed: {tgt_result}"
    yield
    await libvirt_disconnect_host(HostInput(alias=MIGRATION_SOURCE_ALIAS))
    await libvirt_disconnect_host(HostInput(alias=MIGRATION_TARGET_ALIAS))


def _extract_job_id(migration_start_result: str) -> str:
    for line in migration_start_result.splitlines():
        if "Job ID:" in line:
            return line.split("Job ID:", 1)[1].strip()
    raise AssertionError(f"Migration did not return a job ID: {migration_start_result}")


async def _wait_for_migration_job(job_id: str, timeout_seconds: int) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        status_result = await libvirt_get_migration_status(
            MigrationStatusInput(job_id=job_id)
        )
        if "status: succeeded" in status_result:
            return status_result
        if "status: failed" in status_result:
            raise AssertionError(f"Migration job failed:\n{status_result}")
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for migration job '{job_id}'. Last status:\n{status_result}"
            )
        await asyncio.sleep(MIGRATION_POLL_INTERVAL_SECONDS)


async def test_connect_and_disconnect():
    result = await libvirt_connect_host(
        ConnectHostInput(host=INTEGRATION_HOST, alias=ALIAS)
    )
    assert "Error" not in result
    assert INTEGRATION_HOST in result or "Connected" in result
    assert ALIAS in server._connections
    result = await libvirt_disconnect_host(HostInput(alias=ALIAS))
    assert ALIAS not in server._connections


async def test_list_hosts_after_connect(connected):
    result = await libvirt_list_hosts()
    assert ALIAS in result
    assert "live" in result


async def test_list_domains(connected):
    result = await libvirt_list_domains(ListDomainsInput(alias=ALIAS))
    # Should return either a table or "No domains found" — not an error
    assert "Error" not in result


async def test_list_domains_json(connected):
    result = await libvirt_list_domains(
        ListDomainsInput(alias=ALIAS, response_format=ResponseFormat.JSON)
    )
    if "No domains" not in result:
        data = json.loads(result)
        assert "domains" in data
        assert "count" in data


async def test_get_domain_info_for_first_domain(connected):
    list_result = await libvirt_list_domains(
        ListDomainsInput(alias=ALIAS, response_format=ResponseFormat.JSON)
    )
    if "No domains" in list_result:
        pytest.skip("No domains on this host")
    data = json.loads(list_result)
    first_name = data["domains"][0]["name"]
    result = await libvirt_get_domain_info(
        DomainInfoInput(alias=ALIAS, domain=first_name)
    )
    assert first_name in result
    assert "State" in result


async def test_get_domain_xml_for_first_domain(connected):
    list_result = await libvirt_list_domains(
        ListDomainsInput(alias=ALIAS, response_format=ResponseFormat.JSON)
    )
    if "No domains" in list_result:
        pytest.skip("No domains on this host")
    data = json.loads(list_result)
    first_name = data["domains"][0]["name"]
    result = await libvirt_get_domain_xml(DomainInput(alias=ALIAS, domain=first_name))
    assert result.strip().startswith("<domain")
    assert first_name in result


async def test_list_templates():
    result = await libvirt_list_templates()
    assert "default" in result
    assert "suse-leap-micro" in result


TEST_VM_NAME = "mcp-integration-test-vm"


async def test_create_and_cleanup_vm(connected):
    """Create a small VM using default template, verify it exists, then clean up."""
    # Create VM
    result = await libvirt_create_vm(
        CreateVMInput(alias=ALIAS, name=TEST_VM_NAME, disk_size_gb=1, open_viewer=False)
    )
    assert "Error" not in result, f"Create VM failed: {result}"
    assert TEST_VM_NAME in result

    # Verify it appears in domain list
    list_result = await libvirt_list_domains(
        ListDomainsInput(alias=ALIAS, response_format=ResponseFormat.JSON)
    )
    data = json.loads(list_result)
    names = [d["name"] for d in data["domains"]]
    assert TEST_VM_NAME in names

    # Preview delete
    preview = await libvirt_delete_vm(
        DeleteVmInput(alias=ALIAS, domain=TEST_VM_NAME, confirm=False)
    )
    assert "DELETE PREVIEW" in preview
    assert TEST_VM_NAME in preview

    # Delete VM (config + disks)
    result = await libvirt_delete_vm(
        DeleteVmInput(alias=ALIAS, domain=TEST_VM_NAME, confirm=True)
    )
    assert "deleted" in result
    assert "Error" not in result


@requires_two_hosts
async def test_migrate_vm_between_hosts(connected_source_target):
    """Create on source, migrate to target, then clean up source."""
    vm_name = f"mcp-migrate-{uuid4().hex[:8]}"

    try:
        create_result = await libvirt_create_vm(
            CreateVMInput(
                alias=MIGRATION_SOURCE_ALIAS,
                name=vm_name,
                disk_size_gb=1,
                open_viewer=False,
            )
        )
        assert "Error" not in create_result, f"Create VM failed: {create_result}"

        start_result = await libvirt_migrate_vm(
            MigrateVMInput(
                source_alias=MIGRATION_SOURCE_ALIAS,
                target_alias=MIGRATION_TARGET_ALIAS,
                domain=vm_name,
                confirm=False,
            )
        )
        assert "Migration started" in start_result, start_result
        job_id = _extract_job_id(start_result)

        status_result = await _wait_for_migration_job(job_id, MIGRATION_TIMEOUT_SECONDS)
        assert "status: succeeded" in status_result

        cleanup_result = await libvirt_migrate_vm(
            MigrateVMInput(
                source_alias=MIGRATION_SOURCE_ALIAS,
                target_alias=MIGRATION_TARGET_ALIAS,
                domain=vm_name,
                confirm=True,
            )
        )
        assert "cleanup complete" in cleanup_result.lower(), cleanup_result

        target_list_result = await libvirt_list_domains(
            ListDomainsInput(
                alias=MIGRATION_TARGET_ALIAS, response_format=ResponseFormat.JSON
            )
        )
        assert "No domains" not in target_list_result
        target_data = json.loads(target_list_result)
        target_names = [d["name"] for d in target_data["domains"]]
        assert vm_name in target_names

    finally:
        # Best-effort cleanup on both sides to keep labs clean.
        await libvirt_delete_vm(
            DeleteVmInput(alias=MIGRATION_SOURCE_ALIAS, domain=vm_name, confirm=True)
        )
        await libvirt_delete_vm(
            DeleteVmInput(alias=MIGRATION_TARGET_ALIAS, domain=vm_name, confirm=True)
        )
