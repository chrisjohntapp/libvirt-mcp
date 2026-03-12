# libvirt-mcp

An MCP server for managing virtual machines on remote libvirt hosts via SSH.
Designed to run locally on your laptop and connect to one or more hypervisors.

## Requirements

- Python 3.10+
- `libvirt` C libraries installed locally (for `libvirt-python`)
- SSH access to your libvirt host(s) with key-based authentication
- `libvirtd` running on each remote host

### Install libvirt system libraries

**macOS (Homebrew):**
```bash
brew install libvirt
```

**Ubuntu/Debian:**
```bash
sudo apt install libvirt-dev python3-dev
```

**Fedora/RHEL:**
```bash
sudo dnf install libvirt-devel python3-devel
```

## Installation

```bash
cd libvirt-mcp
pip install -e .
```

Or with uv (recommended):
```bash
uv pip install -e .
```

## Usage

### Running the server manually (for testing)

```bash
python server.py
```

The server uses **stdio transport** — it is designed to be launched by a MCP
client (Claude Code, Claude Desktop, etc.), not run as a long-lived daemon.

### Testing with MCP Inspector

```bash
npx @modelcontextprotocol/inspector python server.py
```

## Configuring with Claude Code

Add to your Claude Code MCP config (`~/.claude/claude_mcp_config.json`):

```json
{
  "mcpServers": {
    "libvirt": {
      "command": "python",
      "args": ["/path/to/libvirt-mcp/server.py"]
    }
  }
}
```

Or using `uv run` if you installed via uv:

```json
{
  "mcpServers": {
    "libvirt": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/libvirt-mcp", "python", "server.py"]
    }
  }
}
```

## Available Tools

### Connection Management

| Tool | Description |
|------|-------------|
| `libvirt_connect_host` | Open SSH connection to a libvirt host, register under an alias |
| `libvirt_disconnect_host` | Close connection to a host |
| `libvirt_list_hosts` | Show all active host connections |

### VM Creation

| Tool | Description |
|------|-------------|
| `libvirt_create_vm` | Create a VM from a template (provision disk, define, start) |
| `libvirt_list_templates` | List available VM templates |

### VM Migration

| Tool | Description |
|------|-------------|
| `libvirt_migrate_vm` | Start offline migration as an async job (shutdown, copy disks, define/start target) |
| `libvirt_get_migration_status` | Get migration job status, phase timeline, and final result/error |

Migration flow:
1. Call `libvirt_migrate_vm` with `confirm=false` and capture `job_id`
2. Poll with `libvirt_get_migration_status` until status is `succeeded` or `failed`
3. Call `libvirt_migrate_vm` with `confirm=true` to clean up source definition/disks

### Domain Lifecycle

| Tool | Description | Destructive? |
|------|-------------|--------------|
| `libvirt_list_domains` | List all VMs, optionally filtered by state | No |
| `libvirt_get_domain_info` | Get details for a specific VM | No |
| `libvirt_get_domain_xml` | Retrieve full XML definition of a VM | No |
| `libvirt_define_domain` | Register a new VM from XML | No |
| `libvirt_start_domain` | Boot a shutoff VM | No |
| `libvirt_shutdown_domain` | Send graceful ACPI shutdown signal | No |
| `libvirt_destroy_domain` | Force-stop a VM (power pull) | **Yes** |
| `libvirt_reboot_domain` | Send graceful reboot signal | No |
| `libvirt_suspend_domain` | Pause/freeze a running VM | No |
| `libvirt_resume_domain` | Unpause a suspended VM | No |
| `libvirt_undefine_domain` | Remove VM definition from libvirt | **Yes** |

## Local Environment Config

Create a `.lab/hosts.json` file to define your libvirt hosts (this directory is
git-ignored):

```bash
mkdir -p .lab
```

```json
{
  "hosts": [
    {
      "host": "myhost.example.com",
      "alias": "myhost"
    }
  ]
}
```

Each entry supports the same fields as `libvirt_connect_host`: `host` (required),
`alias` (required), `user` (optional), `port` (optional), `ssh_key_path` (optional).

## Templates

VM templates are JSON files in the `templates/` directory. Two are included:

- `default` -- basic KVM VM with a new empty 10 GB disk
- `suse-leap-micro` -- copies an openSUSE Leap Micro base image

Create your own by adding a JSON file to `templates/`. See existing templates for the schema.

### Creating a VM

> "Create a VM called 'web01' on lab using the default template with 2 vCPUs and 2048 MB RAM"

This provisions the disk on the remote host via SSH, generates domain XML, defines and starts the VM.

## Example conversation with Claude

> **Connect to my lab host:**
> "Connect to my libvirt host at 192.168.1.10 as user ubuntu, alias it 'lab'"

> **List VMs:**
> "List all running VMs on lab"

> **Start a VM:**
> "Start the VM named 'ubuntu-server' on lab"

> **Get XML for editing:**
> "Get the XML for 'ubuntu-server' on lab so I can adjust its memory"

## Extending the server

Future modules to add (each in a separate file, imported into server.py):

- `storage.py` — storage pools, volumes, disk attach/detach
- `networks.py` — virtual networks, interfaces
- `snapshots.py` — snapshot create/list/revert/delete
- `clone.py` — domain cloning helpers

## Security Notes

- Uses SSH for transport — no libvirt TCP port needs to be open
- SSH key path can be specified per host or rely on ssh-agent
- Destructive operations (`destroy`, `undefine`) are annotated accordingly
  so MCP clients can warn before executing them
- Never stores credentials — relies on SSH key infrastructure
