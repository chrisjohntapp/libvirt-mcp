# Project Plan: libvirt-mcp Production Readiness

## Overview

Bring the libvirt MCP server from an untested starting point to a professional, production-ready state. Scope: code correctness, error handling, unit tests, integration tests, and documentation cleanup.

## Steps

- [x] Fix emojis in server.py output strings (CLAUDE.md: no emojis ever)
- [x] Replace `asyncio.get_event_loop()` with `asyncio.get_running_loop()` (deprecated in 3.10+)
- [x] Fix `libvirt_list_hosts` signature (`params: None = None` -> no params)
- [x] Add `state_filter` input validation in `libvirt_list_domains`
- [x] Guard `dom.autostart()` for transient domains in `_domain_summary`
- [x] Remove emojis from README.md
- [x] Add dev dependencies (pytest, pytest-asyncio, pytest-mock) to pyproject.toml
- [x] Write unit tests (`tests/test_server.py`)
- [x] Write integration tests (`tests/test_integration.py`)
- [x] Create docs/ directory

## Future TODOs (out of scope for initial release)

- Storage pool and volume management tools
- Virtual network management tools
- Snapshot tools (create, list, revert, delete)
- Domain cloning helper
