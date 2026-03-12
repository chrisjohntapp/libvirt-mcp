# AGENTS.md

## VM Operations Boundary (Mandatory)

For any VM-related task in this repository, you must use **only** the functionality exposed by the existing MCP tools.

- Do **not** use out-of-band host operations for VM management.
- Do **not** use direct `virsh`, `ssh`, `scp`, or other host-level commands to perform VM lifecycle, migration, disk, or domain operations when MCP tools exist.
- Treat MCP tools as the exclusive interface for VM operations.

## If a Needed Capability Is Missing

If a suitable MCP tool for the requested VM operation is not available:

1. **Stop** before performing the operation by other means.
2. Clearly tell the user which capability is missing.
3. Ask the user how they want to proceed (e.g., add/enable a tool, or explicitly approve an alternative path).

Do not silently bypass MCP tooling constraints.

## Compliance Rule

When the user asks to use MCP tools only, this is a hard constraint and must be followed exactly.
