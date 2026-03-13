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


## Refactor Plan: Split `server.py` into Modules (keep `server.py` shim)

### Goal

Improve maintainability by splitting `server.py` into focused modules while preserving all current behavior, MCP tool names, script entrypoints, and compatibility imports.

### Hard constraints

- Keep `server.py` as a compatibility shim for now.
- Preserve all `@mcp.tool(name="...")` names exactly.
- Preserve request/response behavior and text unless a compatibility fix requires minimal adjustment.
- No feature work during refactor.
- Work incrementally with test validation at each step.
- Use `uv run` for all test execution.

### Current baseline (evidence)

- Monolithic file: `server.py` (~1425 lines), mixing state, models, helpers, SSH operations, and all tools.
- Tests currently import internals from `server` directly:
  - `tests/test_server.py`
  - `tests/test_create_vm.py`
  - `tests/test_migrate_vm.py`
  - `tests/test_integration.py`
- Packaging/script currently points to `server`:
  - `pyproject.toml`: `libvirt-mcp = "server:mcp.run"`

### Proposed target structure

- `libvirt_mcp/__init__.py` (minimal package marker; optional exports)
- `libvirt_mcp/app.py`
  - `mcp = FastMCP("libvirt_mcp")`
- `libvirt_mcp/state.py`
  - `_connections`, `_migration_jobs`, `_migration_jobs_lock`
- `libvirt_mcp/common.py`
  - `_run`, `_format_error`, `_STATE_MAP`, `_domain_state_str`
- `libvirt_mcp/models.py`
  - `ResponseFormat`
  - `_MODEL_CONFIG`, `_ALIAS_FIELD`, `_DOMAIN_FIELD`
  - `ConnectHostInput`, `HostInput`, `DomainInput`, `ListDomainsInput`
  - `DefineVMInput`, `DomainInfoInput`
  - `CreateVMInput`, `DeleteVmInput`
  - `MigrateVMInput`, `MigrationStatusInput`
- `libvirt_mcp/connections.py`
  - `_get_conn`
  - `libvirt_connect_host`, `libvirt_disconnect_host`, `libvirt_list_hosts`
- `libvirt_mcp/domains.py`
  - `_lookup_domain`, `_domain_summary`, `_domain_action`
  - `libvirt_list_domains`, `libvirt_get_domain_info`, `libvirt_get_domain_xml`
  - lifecycle tools: start/shutdown/destroy/reboot/suspend/resume
  - `libvirt_define_domain`, `libvirt_undefine_domain`
- `libvirt_mcp/remote.py`
  - `_ssh_run`, `_parse_uri_parts`, `_find_isos`, `_scp_between_hosts`
- `libvirt_mcp/create_vm.py`
  - `TEMPLATES_DIR`
  - `_load_template`, `_apply_overrides`, `_build_domain_xml`, `_launch_virt_viewer`
  - `libvirt_list_templates`, `libvirt_create_vm`
- `libvirt_mcp/delete_vm.py`
  - `_get_domain_disks`, `libvirt_delete_vm`
- `libvirt_mcp/migration.py`
  - `_rewrite_disk_paths`
  - `_utc_now_iso`
  - job helpers: `_migration_job_create`, `_migration_job_mark_phase`, `_migration_job_mark_running`, `_migration_job_mark_success`, `_migration_job_mark_failure`, `_migration_job_get`, `_run_migration_job`
  - `_migrate_vm_offline`
  - `libvirt_migrate_vm`, `libvirt_get_migration_status`
- `server.py` (shim)
  - imports `mcp` from package and re-exports symbols used by current tests/consumers
  - retains:
    - `if __name__ == "__main__": mcp.run()`

### Symbol move map (mechanical execution checklist)

1. Move shared/core:
   - `_connections`, `_migration_jobs`, `_migration_jobs_lock` -> `state.py`
   - `_run`, `_format_error`, `_STATE_MAP`, `_domain_state_str` -> `common.py`
2. Move models/enums/fields:
   - `ResponseFormat`, `_MODEL_CONFIG`, `_ALIAS_FIELD`, `_DOMAIN_FIELD`
   - all `*Input` classes -> `models.py`
3. Move connection functions/tools:
   - `_get_conn`, `libvirt_connect_host`, `libvirt_disconnect_host`, `libvirt_list_hosts` -> `connections.py`
4. Move domain helpers/tools:
   - `_lookup_domain`, `_domain_summary`, `_domain_action`
   - list/info/xml/lifecycle/define/undefine tools -> `domains.py`
5. Move remote host shell helpers:
   - `_ssh_run`, `_parse_uri_parts`, `_find_isos`, `_scp_between_hosts` -> `remote.py`
6. Move create/template features:
   - `TEMPLATES_DIR`, `_load_template`, `_apply_overrides`, `_build_domain_xml`, `_launch_virt_viewer`
   - `libvirt_list_templates`, `libvirt_create_vm` -> `create_vm.py`
7. Move delete VM:
   - `_get_domain_disks`, `libvirt_delete_vm` -> `delete_vm.py`
8. Move migration:
   - `_rewrite_disk_paths`, job helpers, `_migrate_vm_offline`, migrate/status tools -> `migration.py`
9. Build shim:
   - re-export all symbols currently imported by tests from `server`
   - ensure `mcp` exists at module scope in `server.py`

### Dependency and import rules

- `app.py` should be imported by tool modules for decorator binding (`from libvirt_mcp.app import mcp`).
- Avoid circular imports:
  - shared utilities only in `common.py`, shared mutable state only in `state.py`.
  - tool modules import from `models/common/state/remote` as needed.
- Keep helper names unchanged to minimize test churn.
- Preserve existing docstrings on MCP tool functions.

### Incremental rollout plan (with checkpoints)

Phase 1: Package skeleton + passive shared modules
- Add `libvirt_mcp/` and create `app.py`, `state.py`, `common.py`, `models.py`.
- Update imports in one small slice only.
- Validate: `uv run pytest tests/test_server.py -k "DomainStateStr or FormatError or GetConn or LookupDomain"`

Phase 2: Connections + domain tools
- Move connection and domain tool code.
- Validate: `uv run pytest tests/test_server.py`

Phase 3: Remote helpers + create VM
- Move `remote.py` and `create_vm.py`.
- Validate: `uv run pytest tests/test_create_vm.py`

Phase 4: Delete VM + migration
- Move delete + migration modules.
- Validate: `uv run pytest tests/test_migrate_vm.py`

Phase 5: Shim finalization + full regression
- Replace `server.py` with compatibility shim and re-exports.
- Validate:
  - `uv run pytest`
  - optional integration: `LIBVIRT_TEST_HOST=... uv run pytest tests/test_integration.py -v`

### `server.py` shim contract

`server.py` must continue exporting at least:

- `mcp`
- all tool callables
- all `*Input` models used in tests
- helper functions referenced by tests:
  - `_domain_state_str`, `_format_error`, `_domain_summary`, `_get_conn`, `_lookup_domain`
  - `_load_template`, `_apply_overrides`, `_build_domain_xml`, `_ssh_run`, `_provision_disk`, `_find_isos`, `_launch_virt_viewer`
  - `_get_domain_disks`, `_rewrite_disk_paths`, `_scp_between_hosts`, `_migrate_vm_offline`
- state vars used in tests:
  - `_connections`, `_migration_jobs`

### Risks and mitigations

- Risk: decorator registration drift (tool missing or duplicate).
  - Mitigation: keep tool defs intact; import all tool modules exactly once from shim/bootstrap path.
- Risk: circular imports after split.
  - Mitigation: strict layering (`state/common/models` at base; tools above).
- Risk: tests mocking `server.<name>` break if symbol not re-exported.
  - Mitigation: explicit re-export list in `server.py`; run unit tests after each phase.
- Risk: accidental behavior change in response text.
  - Mitigation: move code first, refactor internals second; keep strings unchanged.

### Done criteria

- All existing unit tests pass with no weakening.
- Optional integration tests pass when host is available.
- `server.py` works as:
  - import surface for current tests/consumers
  - script entrypoint
  - `pyproject.toml` script target unchanged (`server:mcp.run`)
- `server.py` reduced to shim/exports only; core logic lives in `libvirt_mcp/` modules.
