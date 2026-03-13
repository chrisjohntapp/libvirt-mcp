import asyncio

import libvirt


_STATE_MAP = {
    libvirt.VIR_DOMAIN_NOSTATE: "no state",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutting down",
    libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
    libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended (PM)",
}


def _domain_state_str(state_code: int) -> str:
    return _STATE_MAP.get(state_code, f"unknown ({state_code})")


def _format_error(e: Exception, context: str = "") -> str:
    prefix = f"Error ({context}): " if context else "Error: "
    if isinstance(e, (libvirt.libvirtError, ValueError)):
        return f"{prefix}{e}"
    return f"{prefix}{type(e).__name__}: {e}"


async def _run(func, *args):
    """Run a blocking function in the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)
