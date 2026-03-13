#!/usr/bin/env python3
"""Compatibility shim for the libvirt MCP server.

This module preserves the historical import surface while the implementation
is split across `libvirt_mcp` modules.
"""

import logging

from libvirt_mcp import bootstrap  # noqa: F401
from libvirt_mcp.app import mcp
from libvirt_mcp.common import _domain_state_str, _format_error, _run
from libvirt_mcp.connections import (
    _get_conn,
    libvirt_connect_host,
    libvirt_disconnect_host,
    libvirt_list_hosts,
)
from libvirt_mcp.create_vm import (
    _apply_overrides,
    _build_domain_xml,
    _find_isos,
    _launch_virt_viewer,
    _load_template,
    _parse_uri_parts,
    _provision_disk,
    _ssh_run,
    libvirt_create_vm,
    libvirt_list_isos,
    libvirt_list_templates,
)
from libvirt_mcp.delete_vm import _get_domain_disks, libvirt_delete_vm
from libvirt_mcp.domains import (
    _domain_action,
    _domain_summary,
    _lookup_domain,
    libvirt_define_domain,
    libvirt_destroy_domain,
    libvirt_get_domain_info,
    libvirt_get_domain_xml,
    libvirt_list_domains,
    libvirt_reboot_domain,
    libvirt_resume_domain,
    libvirt_shutdown_domain,
    libvirt_start_domain,
    libvirt_suspend_domain,
    libvirt_undefine_domain,
)
from libvirt_mcp.migration import (
    _migrate_vm_offline,
    _rewrite_disk_paths,
    _run_migration_job,
    libvirt_get_migration_status,
    libvirt_migrate_vm,
)
from libvirt_mcp.models import (
    ConnectHostInput,
    CreateVMInput,
    DefineVMInput,
    DeleteVmInput,
    DomainInfoInput,
    DomainInput,
    HostInput,
    ListDomainsInput,
    MigrateVMInput,
    MigrationStatusInput,
    ResponseFormat,
)
from libvirt_mcp.remote import _scp_between_hosts
from libvirt_mcp.state import _connections, _migration_jobs, _migration_jobs_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)


if __name__ == "__main__":
    mcp.run()
