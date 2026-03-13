from enum import Enum

from pydantic import BaseModel, Field


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


_MODEL_CONFIG = {"str_strip_whitespace": True, "extra": "forbid"}

_ALIAS_FIELD = Field(..., description="Host alias", min_length=1, max_length=64)
_DOMAIN_FIELD = Field(
    ..., description="Domain name or UUID", min_length=1, max_length=256
)


class ConnectHostInput(BaseModel):
    model_config = _MODEL_CONFIG

    host: str = Field(
        ...,
        description="Hostname or IP of the libvirt host",
        min_length=1,
        max_length=253,
    )
    alias: str = Field(
        ...,
        description="Short alias for subsequent tool calls (e.g. 'prod')",
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    user: str | None = Field(default=None, description="SSH username", max_length=64)
    port: int | None = Field(default=None, description="SSH port", ge=1, le=65535)
    ssh_key_path: str | None = Field(
        default=None,
        description="Path to SSH private key file",
        max_length=512,
    )


class HostInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD


class DomainInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    domain: str = _DOMAIN_FIELD


class ListDomainsInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    state_filter: str | None = Field(
        default=None,
        description="Filter by state: 'running', 'shutoff', 'paused', 'all' (default: 'all')",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class DefineVMInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    xml: str = Field(
        ..., description="Full libvirt domain XML definition", min_length=10
    )


class DomainInfoInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    domain: str = _DOMAIN_FIELD
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' or 'json'",
    )


class CreateVMInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    name: str = Field(
        ...,
        description="VM name (required -- must be explicitly provided by the user, never auto-generated)",
        min_length=1,
        max_length=64,
    )
    template: str | None = Field(
        default=None, description="Template name (default: 'default')"
    )
    vcpus: int | None = Field(default=None, description="Override vCPUs", ge=1, le=256)
    memory_mb: int | None = Field(
        default=None, description="Override memory in MB", ge=64
    )
    disk_size_gb: int | None = Field(
        default=None, description="Override disk size in GB", ge=1
    )
    network_bridge: str | None = Field(
        default=None, description="Override bridge device"
    )
    boot_iso: str | None = Field(
        default=None, description="ISO path for boot/install media"
    )
    open_viewer: bool = Field(
        default=True, description="Auto-open virt-viewer console after creation"
    )


class DeleteVmInput(BaseModel):
    model_config = _MODEL_CONFIG
    alias: str = _ALIAS_FIELD
    domain: str = _DOMAIN_FIELD
    confirm: bool = Field(
        default=False,
        description="Must be set to true to actually delete. When false, returns a preview of what will be deleted.",
    )


class MigrateVMInput(BaseModel):
    model_config = _MODEL_CONFIG
    source_alias: str = Field(
        ...,
        description="Source host alias (must be connected)",
        min_length=1,
        max_length=64,
    )
    target_alias: str = Field(
        ...,
        description="Target host alias (must be connected)",
        min_length=1,
        max_length=64,
    )
    domain: str = _DOMAIN_FIELD
    shutdown_timeout_seconds: int = Field(
        default=30,
        description="Seconds to wait for graceful shutdown before force-stop",
        ge=1,
        le=3600,
    )
    disk_copy_timeout_seconds: int = Field(
        default=3600,
        description="Seconds allowed for each disk copy before failing migration",
        ge=30,
        le=86400,
    )
    confirm: bool = Field(
        default=False,
        description="false=migrate VM to target; true=clean up source after migration",
    )


class MigrationStatusInput(BaseModel):
    model_config = _MODEL_CONFIG
    job_id: str = Field(
        ..., description="Migration job ID", min_length=1, max_length=128
    )
