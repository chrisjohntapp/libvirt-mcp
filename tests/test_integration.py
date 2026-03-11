"""
Integration tests for server.py against a real libvirt host.

Set LIBVIRT_TEST_HOST to run these tests:
    LIBVIRT_TEST_HOST=lionsteel.coalcreek.lan pytest tests/test_integration.py -v
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
    ConnectHostInput,
    HostInput,
    DomainInput,
    ListDomainsInput,
    DomainInfoInput,
    ResponseFormat,
)

INTEGRATION_HOST = os.environ.get("LIBVIRT_TEST_HOST")
ALIAS = "integration-test"

pytestmark = pytest.mark.skipif(
    not INTEGRATION_HOST,
    reason="Set LIBVIRT_TEST_HOST=lionsteel.coalcreek.lan to run integration tests",
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
