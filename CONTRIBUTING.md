# Contributing to Sunaba

Thank you for contributing! This document guides you through setting up a local development environment and running tests.

---

## 1. Setting Up a Local Development Environment

First, clone the repository and install the package in **editable mode** with testing dependencies:

```bash
git clone https://github.com/masuda-masuo/sunaba.git
cd sunaba

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in editable mode
pip install -e .[test]
```

### Why Editable Mode (`-e`) is Required
`pip install -e .` ensures that imports of `sunaba` resolve directly to your local source code under `src/sunaba/`. 

> [!CAUTION]
> **Do not install a regular non-editable package alongside an editable install.**
> Running a plain `pip install .` and `pip install -e .` together causes Python to resolve imports non-deterministically. It may load stale files from your global or virtual environment's `site-packages` directory instead of reflecting your local edits.

To clean up a mixed installation, uninstall the package repeatedly until pip reports it is not installed, then run the editable installer again:

```bash
pip uninstall sunaba
# Repeat until it says "Not installed"

pip install -e .[test]
```

### Verifying Imports
You can verify that imports correctly resolve to your local source directory by running:

```bash
python -c "import inspect, sunaba.server; print(inspect.getfile(sunaba.server))"
# Expected output should point to your clone:
# /path/to/your/sunaba/src/sunaba/server.py
```

---

## 2. Running Tests

We use `pytest` for unit testing. Make sure your virtual environment is active and run:

```bash
# Run the entire test suite
pytest

# Run tests in verbose mode
pytest -v

# Run specific test suites (e.g., journal logging)
pytest tests/test_journal.py
```

When implementing a new MCP tool or editing an existing one, please ensure that you write or update matching unit tests under the `tests/` directory.
