import asyncio
import copy
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from uuid import uuid4

import libvirt

from libvirt_mcp.app import mcp
from libvirt_mcp.common import _domain_state_str, _format_error, _run
from libvirt_mcp.connections import _get_conn
from libvirt_mcp.delete_vm import _get_domain_disks
from libvirt_mcp.domains import _lookup_domain
from libvirt_mcp.models import MigrateVMInput, MigrationStatusInput
from libvirt_mcp.remote import _parse_uri_parts, _scp_between_hosts, _ssh_run
from libvirt_mcp.state import _migration_jobs, _migration_jobs_lock

logger = logging.getLogger("libvirt_mcp")


def _rewrite_disk_paths(xml: str, path_map: dict[str, str]) -> str:
    """Rewrite disk source paths in domain XML and strip UUID."""
    root = ET.fromstring(xml)
    uuid_elem = root.find("uuid")
    if uuid_elem is not None:
        root.remove(uuid_elem)
    for source in root.findall(".//disk[@device='disk']/source"):
        f = source.get("file")
        if f and f in path_map:
            source.set("file", path_map[f])
    return ET.tostring(root, encoding="unicode")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _migration_job_create(params: MigrateVMInput) -> str:
    job_id = str(uuid4())
    async with _migration_jobs_lock:
        _migration_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "source_alias": params.source_alias,
            "target_alias": params.target_alias,
            "domain": params.domain,
            "created_at": _utc_now_iso(),
            "started_at": None,
            "finished_at": None,
            "phase": "queued",
            "phases": [{"phase": "queued", "at": _utc_now_iso()}],
            "result": None,
            "error": None,
        }
    return job_id


async def _migration_job_mark_phase(job_id: str, phase: str) -> None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return
        job["phase"] = phase
        job["phases"].append({"phase": phase, "at": _utc_now_iso()})


async def _migration_job_mark_running(job_id: str) -> None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return
        now = _utc_now_iso()
        job["status"] = "running"
        job["phase"] = "precheck"
        job["started_at"] = now
        job["phases"].append({"phase": "precheck", "at": now})


async def _migration_job_mark_success(job_id: str, result: str) -> None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return
        now = _utc_now_iso()
        job["status"] = "succeeded"
        job["phase"] = "done"
        job["finished_at"] = now
        job["result"] = result
        job["phases"].append({"phase": "done", "at": now})


async def _migration_job_mark_failure(job_id: str, error: str) -> None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return
        now = _utc_now_iso()
        job["status"] = "failed"
        job["phase"] = "failed"
        job["finished_at"] = now
        job["error"] = error
        job["phases"].append({"phase": "failed", "at": now})


async def _migration_job_get(job_id: str) -> dict | None:
    async with _migration_jobs_lock:
        job = _migration_jobs.get(job_id)
        if job is None:
            return None
        return copy.deepcopy(job)


async def _run_migration_job(job_id: str, params: MigrateVMInput) -> None:
    await _migration_job_mark_running(job_id)
    try:
        result = await _migrate_vm_offline(params, job_id)
    except Exception as e:
        await _migration_job_mark_failure(job_id, _format_error(e, "migrating VM"))
        return
    await _migration_job_mark_success(job_id, result)


async def _migrate_vm_offline(params: MigrateVMInput, job_id: str | None = None) -> str:
    src_conn = _get_conn(params.source_alias)
    tgt_conn = _get_conn(params.target_alias)

    if job_id:
        await _migration_job_mark_phase(job_id, "precheck")

    dom = await _run(lambda: _lookup_domain(src_conn, params.domain))
    name = dom.name()

    try:
        tgt_dom = await _run(lambda: tgt_conn.lookupByName(name))
        src_state = (await _run(dom.info))[0]
        tgt_state = (await _run(tgt_dom.info))[0]
        if src_state == libvirt.VIR_DOMAIN_SHUTOFF and tgt_state in (
            libvirt.VIR_DOMAIN_RUNNING,
            libvirt.VIR_DOMAIN_PAUSED,
        ):
            return (
                f"Migration already completed for '{name}'.\n"
                f"  Target '{params.target_alias}' is {_domain_state_str(tgt_state)}.\n"
                f"  Source '{params.source_alias}' is {_domain_state_str(src_state)}.\n"
                "Source cleanup is still pending. Call again with confirm=true to clean up source."
            )
        return (
            f"Error: Domain '{name}' already exists on target '{params.target_alias}'."
        )
    except libvirt.libvirtError:
        pass

    info = dom.info()
    if info[0] in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED):
        if job_id:
            await _migration_job_mark_phase(job_id, "shutdown")
        timeout_s = params.shutdown_timeout_seconds
        try:
            await _run(dom.shutdown)
            deadline = asyncio.get_running_loop().time() + timeout_s
            while True:
                state = (await _run(dom.info))[0]
                if state == libvirt.VIR_DOMAIN_SHUTOFF:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        f"domain still {_domain_state_str(state)} after {timeout_s}s"
                    )
                await asyncio.sleep(1)
        except Exception as shutdown_error:
            logger.info(
                "Graceful shutdown failed for '%s' (%s); force-stopping",
                name,
                shutdown_error,
            )
            await _run(dom.destroy)

    if job_id:
        await _migration_job_mark_phase(job_id, "collect_domain_xml")
    xml = await _run(dom.XMLDesc, libvirt.VIR_DOMAIN_XML_INACTIVE)
    disks = _get_domain_disks(dom)

    src_host, src_user, src_port, src_key = _parse_uri_parts(src_conn.getURI())
    dst_host, dst_user, dst_port, dst_key = _parse_uri_parts(tgt_conn.getURI())

    for disk_path in disks:
        if job_id:
            await _migration_job_mark_phase(job_id, f"copy_disk:{disk_path}")
        await asyncio.wait_for(
            _scp_between_hosts(
                src_host,
                src_user,
                src_port,
                src_key,
                dst_host,
                dst_user,
                dst_port,
                dst_key,
                disk_path,
                disk_path,
            ),
            timeout=params.disk_copy_timeout_seconds,
        )

    if job_id:
        await _migration_job_mark_phase(job_id, "define_target")
    path_map = {d: d for d in disks}
    new_xml = _rewrite_disk_paths(xml, path_map)
    new_dom = await _run(lambda: tgt_conn.defineXML(new_xml))

    if job_id:
        await _migration_job_mark_phase(job_id, "start_target")
    await _run(new_dom.create)

    disk_list = "\n".join(f"  - {d}" for d in disks) if disks else "  (none)"
    return (
        f"VM '{name}' migrated to '{params.target_alias}'.\n"
        f"  Disks transferred:\n{disk_list}\n"
        f"  UUID on target: {new_dom.UUIDString()}\n\n"
        f"Source still has the old definition and disk files.\n"
        f"Call again with confirm=true to clean up source."
    )


@mcp.tool(name="libvirt_migrate_vm")
async def libvirt_migrate_vm(params: MigrateVMInput) -> str:
    """Offline-migrate a VM from one host to another: stop, copy disks, define+start on target.

    Call with confirm=false to perform the migration.
    Then call with confirm=true to clean up the source (undefine + delete disks).
    """
    try:
        if not params.confirm:
            _get_conn(params.source_alias)
            _get_conn(params.target_alias)
            src_conn = _get_conn(params.source_alias)
            await _run(lambda: _lookup_domain(src_conn, params.domain))
            job_id = await _migration_job_create(params)
            asyncio.create_task(_run_migration_job(job_id, params))
            return (
                f"Migration started for '{params.domain}' from '{params.source_alias}' to '{params.target_alias}'.\n"
                f"  Job ID: {job_id}\n"
                "Use libvirt_get_migration_status with this job_id to track progress."
            )

        src_conn = _get_conn(params.source_alias)
        dom = await _run(lambda: _lookup_domain(src_conn, params.domain))
        name = dom.name()
        info = dom.info()

        if info[0] in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_PAUSED):
            return f"Error: Domain '{name}' is still running on source. Stop it first."

        disks = _get_domain_disks(dom)
        await _run(dom.undefine)

        src_host, src_user, src_port, src_key = _parse_uri_parts(src_conn.getURI())
        deleted = []
        errors = []
        for disk_path in disks:
            try:
                await _ssh_run(
                    src_host, src_user, src_port, src_key, f"sudo rm -f {disk_path}"
                )
                deleted.append(disk_path)
            except Exception as e:
                errors.append(f"{disk_path}: {e}")

        result = f"Source cleanup complete for '{name}' on '{params.source_alias}'.\n"
        if deleted:
            result += (
                "  Disks removed:\n" + "\n".join(f"    - {d}" for d in deleted) + "\n"
            )
        if errors:
            result += (
                "  Disk removal errors:\n"
                + "\n".join(f"    - {e}" for e in errors)
                + "\n"
            )
        if not disks:
            result += "  No disk files to remove.\n"
        return result

    except Exception as e:
        return _format_error(e, f"migrating VM '{params.domain}'")


@mcp.tool(name="libvirt_get_migration_status")
async def libvirt_get_migration_status(params: MigrationStatusInput) -> str:
    """Get status details for a migration started by libvirt_migrate_vm(confirm=false)."""
    job = await _migration_job_get(params.job_id)
    if job is None:
        return f"Error: Migration job '{params.job_id}' not found."

    lines = [
        f"# Migration Job {job['job_id']}",
        "",
        f"- status: {job['status']}",
        f"- phase: {job['phase']}",
        f"- domain: {job['domain']}",
        f"- source: {job['source_alias']}",
        f"- target: {job['target_alias']}",
        f"- created_at: {job['created_at']}",
        f"- started_at: {job['started_at'] or '(pending)'}",
        f"- finished_at: {job['finished_at'] or '(pending)'}",
    ]
    if job["error"]:
        lines.append(f"- error: {job['error']}")
    if job["result"]:
        lines.extend(["", "## Result", job["result"]])
    lines.extend(["", "## Phase Timeline"])
    for entry in job["phases"]:
        lines.append(f"- {entry['at']}: {entry['phase']}")
    return "\n".join(lines)
