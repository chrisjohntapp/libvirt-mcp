# Testing

## Setup

```bash
uv pip install -e ".[dev]"
```

If integration hosts are defined in `.env`, export them into the current shell
before running tests:

```bash
set -a; source .env; set +a
```

## Unit tests

No real libvirt connection required. All libvirt calls are mocked.

```bash
uv run pytest tests/test_server.py -v
```

## Integration tests

Requires SSH access to a libvirt host with key-based auth.

```bash
LIBVIRT_TEST_HOST=your-host.example.com uv run pytest tests/test_integration.py -v
```

`LIBVIRT_TEST_HOST` also supports multiple hosts as a comma-separated list.
This is useful for integration tests that need source and target hosts (for
example, migration workflows):

```bash
LIBVIRT_TEST_HOST=host-a.example.com,host-b.example.com uv run pytest tests/test_integration.py -v
```

## All tests

```bash
# Unit only (CI-safe)
uv run pytest tests/test_server.py

# All including integration
LIBVIRT_TEST_HOST=your-host.example.com uv run pytest tests/ -v

# All including integration (multi-host setup)
LIBVIRT_TEST_HOST=host-a.example.com,host-b.example.com uv run pytest tests/ -v
```

## Troubleshooting skips

If integration tests are skipped with a message about `LIBVIRT_TEST_HOST`, the
variable is not exported in the shell running pytest. Run:

```bash
set -a; source .env; set +a
uv run pytest tests/test_integration.py -v -rs
```

Use `-rs` to show skip reasons.
