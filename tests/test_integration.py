"""
Integration tests for server.py against a real libvirt host.

Set LIBVIRT_TEST_HOST to run these tests:
    LIBVIRT_TEST_HOST=your-host.example.com pytest tests/test_integration.py -v
"""

import os
import json
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
    libvirt_destroy_domain,
    libvirt_undefine_domain,
    libvirt_delete_vm,
    libvirt_list_templates,
    _ssh_run,
    _parse_uri_parts,
    ConnectHostInput,
    HostInput,
    DomainInput,
    DeleteVmInput,
    ListDomainsInput,
    DomainInfoInput,
    CreateVMInput,
    ResponseFormat,
)

INTEGRATION_HOST = os.environ.get("LIBVIRT_TEST_HOST")
ALIAS = "integration-test"

pytestmark = pytest.mark.skipif(
    not INTEGRATION_HOST,
    reason="Set LIBVIRT_TEST_HOST to run integration tests",
)


@pytest.fixture(autouse=True)
def clear_connections():
    server._connections.clear()
    yield
    server._connections.clear()


@pytest.fixture
async def connected():
    """Establish a real connection before each test and clean up after."""
    result = await libvirt_connect_host(ConnectHostInput(host=INTEGRATION_HOST, alias=ALIAS))
    assert "Error" not in result, f"Connection failed: {result}"
    yield
    await libvirt_disconnect_host(HostInput(alias=ALIAS))


async def test_connect_and_disconnect():
    result = await libvirt_connect_host(ConnectHostInput(host=INTEGRATION_HOST, alias=ALIAS))
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
    preview = await libvirt_delete_vm(DeleteVmInput(alias=ALIAS, domain=TEST_VM_NAME, confirm=False))
    assert "DELETE PREVIEW" in preview
    assert TEST_VM_NAME in preview

    # Delete VM (config + disks)
    result = await libvirt_delete_vm(DeleteVmInput(alias=ALIAS, domain=TEST_VM_NAME, confirm=True))
    assert "deleted" in result
    assert "Error" not in result
