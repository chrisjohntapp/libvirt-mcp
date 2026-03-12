# Project Plan

## Feature: Create VM (high-level tool)

A single `libvirt_create_vm` tool that takes a VM name, an optional template,
and optional overrides -- then provisions storage, generates XML, defines the
domain, and starts it.

### Design

**New tool**: `libvirt_create_vm`

Parameters:
- `alias` (required) -- host alias (existing pattern)
- `name` (required) -- VM name
- `template` (optional) -- template name; defaults to `"default"`
- `vcpus` (optional) -- override template vcpus
- `memory_mb` (optional) -- override template memory
- `disk_size_gb` (optional) -- override disk size (only for new-disk templates)
- `network_bridge` (optional) -- override bridge device; defaults to `br0`
- `boot_iso` (optional) -- ISO path for boot/install media (CDROM)

**Templates**: stored as JSON files in `templates/` directory at project root.
Each template defines a base VM spec. Users can add/edit templates manually.

Template schema:
```json
{
  "description": "openSUSE Leap Micro 6.1",
  "vcpus": 2,
  "memory_mb": 2048,
  "disk": {
    "source": "copy",
    "source_path": "/var/lib/libvirt/images/openSUSE-Leap-Micro.x86_64-Default-qcow.qcow2",
    "bus": "virtio"
  },
  "os": {
    "type": "hvm",
    "arch": "x86_64",
    "boot_dev": "hd"
  },
  "network_bridge": "br0"
}
```

Disk source types:
- `"copy"` -- copy an existing qcow2 from `source_path` to
  `/var/lib/libvirt/images/<vm-name>.qcow2`
- `"create"` -- create a new empty qcow2 of `disk_size_gb`
  at `/var/lib/libvirt/images/<vm-name>.qcow2`

**New tool**: `libvirt_list_templates`

Parameters:
- (none)

Returns: list of available template names with descriptions.

**Storage provisioning** (runs on remote host via SSH):

For `"copy"` disk source:
- SSH to host, run `cp <source_path> /var/lib/libvirt/images/<name>.qcow2`

For `"create"` disk source:
- SSH to host, run `qemu-img create -f qcow2 /var/lib/libvirt/images/<name>.qcow2 <size>G`

Note: We use SSH commands rather than libvirt storage pool APIs for simplicity.
The connection URI already provides SSH access to the host. We'll use
`asyncio.create_subprocess_exec` with ssh to run commands on the remote host.

**XML generation**: Build domain XML from template + overrides using string
formatting or xml.etree. Keep it simple -- a Python function that takes the
resolved spec dict and returns XML string.

### File structure

```
templates/
  default.json          -- basic KVM VM (create new disk)
  suse-leap-micro.json  -- copy openSUSE Leap Micro image
server.py               -- add libvirt_create_vm + libvirt_list_templates tools
                           add _load_template(), _provision_disk(), _build_domain_xml()
tests/
  test_create_vm.py     -- unit tests for create VM feature
  test_server.py        -- existing (unchanged)
  test_integration.py   -- add integration test for create VM
```

### Implementation steps (TDD -- tests first)

#### Step 1: Templates infrastructure

Tests first (`test_create_vm.py`):
- `TestLoadTemplate`: test loading a valid template, missing template, default
  template, template with overrides applied
- `TestListTemplates`: test listing available templates

Then implement:
- `_load_template(name)` function that reads from `templates/` directory
- `_apply_overrides(template, overrides)` to merge user overrides
- `libvirt_list_templates` tool

Success criteria: `uv run pytest tests/test_create_vm.py::TestLoadTemplate -v` passes.

#### Step 2: XML generation

Tests first:
- `TestBuildDomainXml`: test XML output for a basic spec (name, vcpus, memory,
  disk path, bridge), test with ISO CDROM, test with different arch

Then implement:
- `_build_domain_xml(spec)` function using xml.etree.ElementTree

Success criteria: `uv run pytest tests/test_create_vm.py::TestBuildDomainXml -v` passes.

#### Step 3: Disk provisioning

Tests first:
- `TestProvisionDisk`: test "create" mode builds correct qemu-img command,
  test "copy" mode builds correct cp command, test error handling for failed
  SSH command

Then implement:
- `_provision_disk(host, user, port, ssh_key, disk_spec, vm_name)` function
  that runs remote commands via SSH subprocess
- Helper `_ssh_run(host, user, port, ssh_key, command)` for remote execution

Success criteria: `uv run pytest tests/test_create_vm.py::TestProvisionDisk -v` passes.

#### Step 4: Create VM tool (integration of steps 1-3)

Tests first:
- `TestCreateVm`: test full happy path (mock template load, disk provision,
  defineXML, create), test with overrides, test with boot ISO, test error
  cases (template not found, disk provision fails, defineXML fails)

Then implement:
- `libvirt_create_vm` tool wiring everything together:
  1. Load template + apply overrides
  2. Provision disk on remote host
  3. Build XML
  4. `conn.defineXML(xml)`
  5. `dom.create()` (start)
  6. Return success message with VM details

Success criteria: `uv run pytest tests/test_create_vm.py -v` all pass.

#### Step 5: Integration test

- Add test in `test_integration.py` that creates a small VM on the test host
  using the default template, verifies it appears in domain list, then
  cleans up (destroy + undefine + remove disk).

Success criteria: Integration test passes against real host.

#### Step 6: Documentation

- Update README with new tools
- Add example templates to `templates/`

### Default templates to ship

1. `default.json` -- 1 vcpu, 1024 MB RAM, create new 10 GB qcow2, boot hd,
   bridge br0
2. `suse-leap-micro.json` -- 2 vcpus, 2048 MB RAM, copy from
   `/var/lib/libvirt/images/openSUSE-Leap-Micro.x86_64-Default-qcow.qcow2`,
   boot hd, bridge br0


## Future TODOs (out of scope for initial release)

- Storage pool and volume management tools
- Virtual network management tools
- Snapshot tools (create, list, revert, delete)
- Domain cloning helper
- VM migration tool
