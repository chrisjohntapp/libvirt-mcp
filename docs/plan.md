# Project Plan

## Current State

The MCP server is functional with the following tools implemented and tested:

### Connection management
- `libvirt_connect_host` -- connect to a remote libvirt host via SSH
- `libvirt_disconnect_host` -- close a connection
- `libvirt_list_hosts` -- list active connections

### Domain lifecycle
- `libvirt_list_domains` -- list VMs (with optional state filter, markdown/json output)
- `libvirt_get_domain_info` -- detailed info for a single domain
- `libvirt_get_domain_xml` -- raw XML definition
- `libvirt_start_domain`
- `libvirt_shutdown_domain` -- graceful ACPI shutdown
- `libvirt_destroy_domain` -- force stop
- `libvirt_reboot_domain`
- `libvirt_suspend_domain`
- `libvirt_resume_domain`
- `libvirt_define_domain` -- define from raw XML
- `libvirt_undefine_domain` -- remove domain definition (keeps disks)

### VM creation and deletion
- `libvirt_create_vm` -- create VM from template with optional overrides
- `libvirt_delete_vm` -- destroy + undefine + delete disk files (with confirm/preview)
- `libvirt_list_templates` -- list available VM templates

### VM migration
- `libvirt_migrate_vm` -- offline migration started as an async job (stop, copy disks, define+start on target)
- `libvirt_get_migration_status` -- check async migration status, current phase, timeline, and result/error

### ISO management
- `libvirt_list_isos` -- list ISOs in /var/lib/libvirt/images/ on a host

### Key implementation details

**Templates** (`templates/` directory):
- `default.json` -- 1 vcpu, 1024 MB RAM, create new 10 GB qcow2, boot hd, bridge br0
- `suse-leap-micro.json` -- 2 vcpus, 2048 MB RAM, copy openSUSE Leap Micro image

**ISO discovery** (in `libvirt_create_vm`):
- When `boot_iso` is a partial/fuzzy name (not an absolute path), the server searches
  `/var/lib/libvirt/images/*.iso` on the host for matches (all words, case-insensitive).
- Single match: used automatically. Multiple matches: listed for user to choose.
  No matches: error with list of available ISOs.

**Storage provisioning** via SSH:
- `"create"` -- `qemu-img create -f qcow2` on remote host
- `"copy"` -- `cp` from source path on remote host

**Lab hosts**: defined in `.lab/hosts.json` (gitignored).

### Test coverage

- `tests/test_server.py` -- unit tests for connection management, domain lifecycle, delete VM
- `tests/test_create_vm.py` -- unit tests for templates, XML generation, disk provisioning,
  create VM, ISO discovery (39 tests)
- `tests/test_migrate_vm.py` -- unit tests for VM migration: XML rewriting, scp helpers,
  migrate tool async job lifecycle + cleanup (22 tests)
- `tests/test_integration.py` -- integration tests against real libvirt host (via LIBVIRT_TEST_HOST)

All 121 unit tests passing.


## Completed features

1. Core domain management tools (connect, list, start, stop, etc.)
2. Create VM from template with overrides, boot ISO, virt-viewer launch
3. Delete VM with preview/confirm, disk cleanup
4. ISO discovery -- fuzzy matching for boot_iso parameter
5. `libvirt_list_isos` tool
6. VM migration -- offline cold migration between hosts with confirm pattern


## Future TODOs (out of scope for initial release)

- Storage pool and volume management tools
- Virtual network management tools
- Snapshot tools (create, list, revert, delete)
- Domain cloning helper
