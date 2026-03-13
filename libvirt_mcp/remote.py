import asyncio
import getpass
import logging
import urllib.parse

logger = logging.getLogger("libvirt_mcp")


async def _ssh_run(
    host: str, user: str, port: int, ssh_key: str | None, command: str
) -> str:
    """Run a command on a remote host via SSH."""
    args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
    if ssh_key:
        args.extend(["-i", ssh_key])
    args.extend(["-p", str(port), f"{user}@{host}", command])
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"SSH command failed (rc={proc.returncode}): {stderr.decode()}"
        )
    return stdout.decode()


def _parse_uri_parts(uri: str) -> tuple[str, str, int, str | None]:
    """Extract host, user, port, ssh_key from a qemu+ssh URI."""
    parsed = urllib.parse.urlparse(uri)
    host = parsed.hostname or ""
    user = parsed.username or getpass.getuser()
    port = parsed.port or 22
    qs = urllib.parse.parse_qs(parsed.query)
    ssh_key = qs.get("keyfile", [None])[0]
    if ssh_key:
        ssh_key = urllib.parse.unquote(ssh_key)
    return host, user, port, ssh_key


async def _find_isos(
    host: str,
    user: str,
    port: int,
    ssh_key: str | None,
    pattern: str,
) -> list[str]:
    """Find ISOs in /var/lib/libvirt/images/ matching pattern (all words, case-insensitive)."""
    output = await _ssh_run(
        host,
        user,
        port,
        ssh_key,
        "ls /var/lib/libvirt/images/*.iso 2>/dev/null || true",
    )
    all_isos = [line.strip() for line in output.splitlines() if line.strip()]
    words = pattern.lower().split()
    return [iso for iso in all_isos if all(w in iso.lower() for w in words)]


async def _scp_between_hosts(
    src_host: str,
    src_user: str,
    src_port: int,
    src_key: str | None,
    dst_host: str,
    dst_user: str,
    dst_port: int,
    dst_key: str | None,
    src_path: str,
    dst_path: str,
) -> None:
    """Copy a file between two remote hosts. Tries direct scp, falls back to local relay."""
    scp_cmd = (
        f"sudo scp -o StrictHostKeyChecking=no "
        f"-P {dst_port} {src_path} {dst_user}@{dst_host}:{dst_path}"
    )
    try:
        await _ssh_run(src_host, src_user, src_port, src_key, scp_cmd)
        return
    except RuntimeError:
        logger.info("Direct scp failed, falling back to local relay")

    src_ssh = "ssh -o StrictHostKeyChecking=no -o BatchMode=yes"
    if src_key:
        src_ssh += f" -i {src_key}"
    src_ssh += f" -p {src_port} {src_user}@{src_host} 'sudo cat {src_path}'"

    dst_ssh = "ssh -o StrictHostKeyChecking=no -o BatchMode=yes"
    if dst_key:
        dst_ssh += f" -i {dst_key}"
    dst_ssh += f" -p {dst_port} {dst_user}@{dst_host} 'sudo tee {dst_path} > /dev/null'"

    cmd = f"{src_ssh} | {dst_ssh}"
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"Relay transfer failed: {stderr.decode()}")
