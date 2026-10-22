# Contributing to nomothetic

## Development Environment

### Requirements

- Python 3.11+
- Windows, macOS, or Linux (tests must pass on all three)
- Hardware is mocked in tests — no Raspberry Pi required for development

### Setup

```bash
git clone https://github.com/Perceptua-Nomon/nomothetic.git
cd nomothetic
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -e ".[dev,web,api]"
```

Or with the Makefile:

```bash
make install-dev
```

---

## Running Tests

```bash
make test
# or
pytest --cov=nomothetic --cov-report=term-missing
```

All 63 tests must pass before submitting changes. Tests run entirely with mocked hardware — no Pi required.

### Test Structure

```
tests/
  test_camera.py      # 20 tests — Camera class (picamera2 mocked)
  test_streaming.py   # 14 tests — StreamServer (Flask mocked)
  test_api.py         # 26 tests — APIServer (camera mocked)
  __init__.py         # 3 integration tests
```

### Writing Tests

- Mock all hardware (`picamera2`, GPIO, Flask, uvicorn) using `unittest.mock`
- Tests must pass on Windows/macOS — no platform-specific test code
- Use `pytest.mark` markers for any tests that genuinely require hardware
- Target: every public method has at least one success and one failure test

---

## Code Style

### Formatter — black

```bash
make format
# or
black .
```

Line length: 100. Do not manually wrap lines shorter than 100 characters.

### Linter — ruff

```bash
make lint
# or
ruff check .
```

Active rules: pycodestyle (E/W), Pyflakes (F), isort (I), flake8-comprehensions (C4), bugbear (B), pyupgrade (UP).

### Type Checker — mypy

```bash
make type-check
# or
mypy src/
```

- All functions and methods must have full type hints including return types
- Do not use bare `# type: ignore` without a comment explaining why
- `# type: ignore` is acceptable only for unavoidable platform-conditional symbols set to `None`

---

## Code Conventions

### Docstrings

All public classes, methods, and functions use **NumPy-style docstrings**:

```python
def capture_image(self, filename: str) -> None:
    """Capture a still image from the camera.

    Parameters
    ----------
    filename : str
        Plain filename (no path components) for the captured image.

    Raises
    ------
    ValueError
        If filename contains path separators or other invalid characters.
    RuntimeError
        If the camera fails to capture.
    """
```

### Platform-Conditional Imports

Linux-only dependencies use `try/except ImportError` at module level. The symbol is set to `None` and checked at runtime — never at import time:

```python
try:
    from picamera2 import Picamera2
except ImportError:
    Picamera2 = None  # type: ignore

class Camera:
    def __init__(self) -> None:
        if Picamera2 is None:
            raise RuntimeError("picamera2 not available — requires Raspberry Pi")
```

### Exception Handling

- Always chain exceptions: `raise NewError("msg") from e`
- Never use `raise ... from None` unless deliberately suppressing context
- Never catch bare `Exception` without re-raising or logging
- Use specific exception types (`ValueError`, `RuntimeError`, `OSError`)

### Logging

- Use the Python `logging` module — no `print()` calls in library code
- Logger name should match the module: `logging.getLogger(__name__)`
- Library code must not configure handlers — leave that to the application

### Async Code

- FastAPI route handlers are `async def` — do not call blocking functions directly inside them
- Wrap blocking calls in `asyncio.to_thread()` or run them in a background thread

### Security

- Never accept path-like filenames from external input
- Validate filenames as plain names only: no `/`, `\`, `..`, leading `.`
- All files must be written within the configured `directory` — never outside it
- Never hardcode credentials, tokens, or secrets
- Use `python-dotenv` to load env files; `.env.device` and `.env.central` are gitignored — never commit them

---

## Adding a New Module

1. Create `src/nomothetic/<module>.py` — one class per file is the norm
2. Add conditional imports for any Linux-only dependencies
3. Raise `RuntimeError` at instantiation if required hardware is unavailable
4. Export the class from `src/nomothetic/__init__.py` (with `try/except` if optional)
5. Create `tests/test_<module>.py` with mocked hardware
6. Update `docs/architecture.md` with the new component
7. Update `pyproject.toml` optional-dependencies if new packages are required

---

## Adding REST Endpoints

1. Define Pydantic request/response models near the top of `api.py`
2. Add the route function inside `create_app()`
3. Include UTC timestamp in every response
4. Use `400` for bad input, `409` for conflict states, `500` for hardware errors
5. Write tests in `test_api.py` covering success, validation failure, and hardware error paths

---

## Commit Style

Use short, descriptive imperative commit messages:

```
Add HAT driver for XYZ-1234 module
Fix path traversal check missing backslash on Windows
Bump FastAPI to 0.115
```

---

## Project Checks Before PR

```bash
make format       # black formatting
make lint         # ruff clean
make type-check   # mypy clean
make test         # all tests pass
```
