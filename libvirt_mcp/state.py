import asyncio

import libvirt

_connections: dict[str, libvirt.virConnect] = {}
_migration_jobs: dict[str, dict] = {}
_migration_jobs_lock = asyncio.Lock()
