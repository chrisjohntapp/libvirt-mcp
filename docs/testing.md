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

Requires SSH access to `lionsteel.coalcreek.lan` with key-based auth.

```bash
LIBVIRT_TEST_HOST=lionsteel.coalcreek.lan pytest tests/test_integration.py -v
```

## All tests

```bash
# Unit only (CI-safe)
pytest tests/test_server.py

# All including integration
LIBVIRT_TEST_HOST=lionsteel.coalcreek.lan pytest tests/ -v
```
