# Suggested Improvements for bitbrew

## 1. Add Input Validation for Edge Cases

Several CLI arguments lack validation, which can cause confusing behavior or hangs:

- **`--chunk-size 0` causes an infinite loop** in `_chunked_write` (`bitbrew.py:206-240`)
  because the loop never advances. Add a check that `chunk_size > 0`.
- **`--min-len > --max-len`** silently produces 0 results (`bitbrew.py:265-266`).
  Should warn the user or raise an error.
- **Empty charset** (e.g. `--charset ""`) silently returns nothing.
  Should produce a clear error message.

## 2. Expand Test Coverage for Edge Cases and Error Paths

The test suite (36 tests) covers happy paths well but is missing important scenarios:

- No test for `chunk_size <= 0` (the infinite loop bug)
- No test for invalid `min_len > max_len` combinations
- No test for empty charset handling
- No test for gzip file corruption or write-permission errors
- No test for the interrupt/cleanup path (partial file removal on Ctrl+C)
- No stress/performance test for large pattern spaces

## 3. Add Project Configuration (`pyproject.toml`)

The project has no packaging metadata — no `pyproject.toml`, `setup.cfg`, or
`requirements.txt`. This means:

- Optional dependency `tqdm` is undocumented except in prose
- Dev dependency `pytest` isn't formally declared
- There's no way to `pip install .` or `pip install -e .` for development
- No CLI entry point — users must run `python3 bitbrew.py` instead of `bitbrew`

A minimal `pyproject.toml` would solve all of these.

## 4. Set Up CI/CD (GitHub Actions)

There is no automated testing pipeline. A GitHub Actions workflow that runs
`pytest` on push/PR would catch regressions early and is trivial to set up for
a pure-Python project with no external dependencies.

## 5. Guard Against ReDoS in `--filter`

User-supplied regex patterns (`bitbrew.py:304`) are compiled and applied to
every generated word with no complexity check or timeout. A pathological pattern
like `(a+)+$` could cause extreme CPU usage. Consider adding a timeout wrapper
or a basic complexity heuristic.
