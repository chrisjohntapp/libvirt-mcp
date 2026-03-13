# Testing

## Setup

```bash
pip install -e ".[dev]"
```

## Unit tests

No real libvirt connection required. All libvirt calls are mocked.

```bash
pytest tests/test_server.py -v
```

## Integration tests

Requires SSH access to a libvirt host with key-based auth.

```bash
LIBVIRT_TEST_HOST=your-host.example.com pytest tests/test_integration.py -v
```

`LIBVIRT_TEST_HOST` also supports multiple hosts as a comma-separated list.
This is useful for integration tests that need source and target hosts (for
example, migration workflows):

```bash
LIBVIRT_TEST_HOST=host-a.example.com,host-b.example.com pytest tests/test_integration.py -v
```

## All tests

```bash
# Unit only (CI-safe)
pytest tests/test_server.py

# All including integration
LIBVIRT_TEST_HOST=your-host.example.com pytest tests/ -v

# All including integration (multi-host setup)
LIBVIRT_TEST_HOST=host-a.example.com,host-b.example.com pytest tests/ -v
```
