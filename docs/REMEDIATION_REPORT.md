# Code review and remediation report

**Scope:** the whole repository at `d8c91e6`: `bitbrew.py` (1,845 lines), `test_bitbrew.py`
(2,697 lines, 248 tests), `README.md`, `pyproject.toml`, `setup.py`, and `.github/workflows/ci.yml`.

**Baseline:** 339 test cases pass in 15 s, coverage is 96.5% (the gate is 93%), and
`ruff check .` and `mypy --strict` are both clean. The previous two audit rounds (see
`ROADMAP.md`) left the code in good shape. Every finding below is new, and each was
reproduced against the current head. The repro commands are included so each fix can start
from a failing test.

**Security sweep:** there are no hardcoded secrets, no subprocess calls in production code
(`shell=True` appears nowhere), and no credential logging. External input reaches the tool as
the `--filter` regex, file paths and charset text. The security-relevant problems are H1
(clobbering non-regular files), M2 and M3 (gaps in the ReDoS screen), and the mode change in H1.

## Summary

| ID | Severity | Area | Finding |
|----|----------|------|---------|
| H1 | High | Output placement | `--overwrite` replaces device nodes, FIFOs and symlinks with a regular file, and drops the existing file's mode |
| M1 | Medium | Error handling | Encoding errors escape as tracebacks and leak the `.part` sidecar |
| M2 | Medium | ReDoS screen | An anchored literal prefix such as `^pass(\d\|\d)+$` passes both layers |
| M3 | Medium | ReDoS screen | Screening has no bound on its own cost: the first probe of a 14-way alternation takes 60 s |
| M4 | Medium | Counting | Estimates saturate at 10^15, so `--count` and `estimate_count()` give wrong answers |
| M5 | Medium | Input handling | Newlines in the charset or pattern corrupt the line-oriented output, and `--charset-file` rewrites `\r` |
| L1 | Low | Docs / interrupt | The README says an interrupted `--compress` stream is a valid archive; it is corrupt |
| L2 | Low | Memory | `--chunk-size` has no upper bound on the stdout path (795 MB RSS at 20M) |
| L3 | Low | Messaging | The exact-dedup memory warning ignores `--limit` |
| L4 | Low | Interrupt | Ctrl-C during option resolution or regex screening prints a traceback |
| L5 | Low | UX | `-o <directory>` suggests `--overwrite`, which then fails; `-o -` creates a file named `-` |
| L6 | Low | Packaging | `license = "MIT"` needs setuptools 77 or later, but the floor is 68 |
| L7 | Low | CI | Python 3.10 reaches end of life this month, 3.14 is untested, and actions and linters are unpinned |
| L8 | Low | Tests / screen | Wall-clock assertions and an absolute-time probe budget are nondeterministic under load |
| I1–I4 | Info | Various | Reproducibility, documentation and maintainability notes |

Suggested order: H1, M1, M5 (data integrity), then M3, M2 and L4 (screening), then M4, then
the rest.

---

## High

### H1. `--overwrite` replaces non-regular files and drops permissions

**Where:** `_place_output` (`bitbrew.py:872-874`, and the fallback at `882-886`). Also the
guard in `_resolve_options` (`bitbrew.py:1414`), which tests only `os.path.exists`.

With `--overwrite`, the finished sidecar is placed with `os.replace(temp, output_path)`. That
replaces the **directory entry**, whatever it points at:

| Existing `-o` target | What a plain `open(path, "w")` does | What bitbrew does |
|---|---|---|
| Character device (`/dev/null`) | Writes into the device | **Replaces the device node with a regular file** |
| FIFO | Feeds the reader | Replaces the FIFO; the reader never gets the data |
| Symlink | Writes the link's target | Replaces the link; the target is unchanged |
| Regular file, mode 0600 | Keeps mode 0600 | New file at `0666 & ~umask` (0644) |

Reproduced (as root, which is how this container runs and how Kali-style pentest VMs often
run):

```console
$ mknod fakenull c 1 3; mkfifo pipe; ln -s target link; echo s > priv; chmod 600 priv
$ bitbrew -p "*" --charset ab -o fakenull --overwrite   # crw-r--r-- -> -rw-r--r--
$ bitbrew -p "*" --charset ab -o pipe --overwrite       # prw-r--r-- -> -rw-r--r--
$ bitbrew -p "*" --charset ab -o link --overwrite       # symlink -> regular file
$ bitbrew -p "*" --charset ab -o priv --overwrite       # 600 -> 644
```

As root, `bitbrew ... -o /dev/null --overwrite`, a common way to benchmark a run, replaces
the system's `/dev/null` with a regular file. Every later process that writes to it then
appends to a file on disk. The mode change matters too: a wordlist kept at 0600 because it
holds real credentials becomes readable by everyone after a regeneration.

**Remediation:**
1. In `_resolve_options`, when the output path exists, `os.stat()` it (following symlinks).
   If it is not `S_ISREG`, the atomic sidecar strategy does not apply. Either write to it
   directly, with no sidecar and no rename (the right call for `/dev/null`, `/dev/stdout`
   and FIFOs), or refuse with a clear message. Direct writing is the least surprising choice.
2. For a symlink to a regular file, resolve it (`os.path.realpath`) and place the sidecar
   next to the **target**, so the link survives and the target is updated.
3. When replacing an existing regular file, copy its mode onto the sidecar before placement
   (`os.chmod(temp, stat.S_IMODE(st.st_mode))`). Restoring ownership needs root, so document
   that it is not kept.

**Tests:** create a FIFO, a symlink and a 0600 file under `tmp_path`. Assert that the FIFO is
still a FIFO and its reader received the words, that the link is still a link with the
target updated, and that the mode is still 0600. For the device case, `mknod` under
`tmp_path`, skipped unless running as root.

---

## Medium

### M1. Encoding errors escape as tracebacks and leak the sidecar

**Where:** `_write_to_file` (`bitbrew.py:1608-1631`) handles only `KeyboardInterrupt`,
`FileExistsError` and `OSError`. `_stream_to_stdout` (`bitbrew.py:1737-1749`) has the same
gap.

On Linux, argv bytes that are not valid UTF-8 arrive as lone surrogates (`\udcff`). The
sidecar is opened with `encoding="utf-8"` (strict), so the first write raises
`UnicodeEncodeError`, which is a `ValueError` rather than an `OSError`:

```console
$ bitbrew -p "*" --charset "$(printf 'a\xff')" -o out.txt
Traceback (most recent call last): ...
UnicodeEncodeError: 'utf-8' codec can't encode character '\udcff' ...
$ ls
out.txt.22fd88c450031533.part         # left behind, 0 bytes
```

Stdout has the same problem whenever its encoding cannot represent the charset:

```console
$ PYTHONIOENCODING=ascii bitbrew -p "*" --charset "é"
UnicodeEncodeError: 'ascii' codec can't encode character '\xe9' ...
```

**Remediation:**
1. Validate early. In `_resolve_options`, try `charset.encode("utf-8")` and each
   `pattern.encode("utf-8")`. On failure, raise
   `_CliError("charset contains bytes that are not valid UTF-8")`. For stdout, check the
   charset and literals against `sys.stdout.encoding` and give a clear error, or reconfigure
   stdout with `errors="surrogateescape"` so arbitrary bytes round-trip, which is arguably
   what a wordlist tool should do.
2. Make cleanup unconditional. Track `placed = False` and remove the sidecar in the
   `finally:` block when `not placed`, rather than repeating `_remove_if_exists` in each
   `except`. Any unexpected exception, and a `KeyboardInterrupt` landing inside an `except`
   block, then still cleans up.

**Tests:** call `main()` with `"\udcff"` in `--charset` and `-o`, then assert exit code 1, a
readable error and `_sidecars(tmp_path) == []`. Do the same for a stdout replaced by an
ASCII-only `io.TextIOWrapper`.

### M2. ReDoS probe misses patterns with a required literal prefix

**Where:** `_probe_seeds` and `_probe_regex_blowup` (`bitbrew.py:453-558`).

Every probe string is one repeated seed (`"aaaa…\0"`). A pattern that requires a literal
before its vulnerable group fails on the first character of every probe, so the probe sees
fast rejections and passes it. The structural check does not flag alternation either:

```text
^x(a|a)+$        structural=ok  probe=ok  (screen 0.1 ms)
^pass(\d|\d)+$   structural=ok  probe=ok  (screen 0.1 ms)
^-(a|a)+$        structural=ok  probe=ok
```

These are real hangs on words the generator can emit. `^pass(\d|\d)+$` takes 0.40 s on a
27-character word, and each extra character doubles that. An anchored literal prefix is the
most common shape a real `--filter` takes, so this is the gap most likely to bite.
(`^ab(a|b|ab)*$` is caught only because the two-character seed happens to be `ab`.)

**Remediation:** build each probe as *prefix + seed × n + breaker*, where the prefix is the
literal text the pattern requires before its first quantified group. `_probe_seeds` already
walks the source, so it can collect plain literals that appear before the first repeated
group, in source order, into a `prefix`. Also try a probe with no prefix, for unanchored
patterns. That at most doubles the probe count, which is still well under a millisecond for
safe patterns.

**Tests:** add `^x(a|a)+$`, `^pass(\d|\d)+$` and `^-(a|a)+$` to the "rejected" parametrisation
in `TestRegexBlowupProbe`.

### M3. The screen's own cost is unbounded

**Where:** `_PROBE_LENGTHS = range(8, 41, 2)` (`bitbrew.py:306`). The budget check sits
*after* each `regex.search` (`bitbrew.py:551-553`).

The ladder starts at length 8, and the 50 ms budget is checked only once a search returns.
For an alternation of *k* identical branches the first search alone costs about *k*⁸ steps:

| Branches | First 8-character probe |
|---|---|
| 8 | 0.8 s |
| 10 | 4.4 s |
| 12 | 17.8 s |
| 14 | 60.2 s |

The README says the probe "stops after a match pushes its cumulative work over the time
budget". That is true, but one match can overshoot the budget by minutes. Screening runs
before the interrupt guard is installed, so Ctrl-C during that wait also prints a traceback
(see L4).

**Remediation:** start the ladder short and climb in steps of one (`range(1, 41)`). With step
1 the overshoot past the budget is at most one factor of the per-character growth rate, not
eight. For a hard guarantee, run the probe in a worker (`multiprocessing` with a timeout, or
a `signal.setitimer` alarm on the main thread) and treat a timeout as unsafe.

**Tests:** assert that screening `"(" + "|".join(["a"] * 14) + ")+$"` is rejected in under 1 s.

### M4. Estimates saturate at 10^15

**Where:** `_MAX_ESTIMATE` (`bitbrew.py:158`), `_count_from_kinds` (`163-183`),
`exact_output_count` (`1181`), `main` (`1808-1820`).

`_count_from_kinds` clamps to 10^15, and `exact_output_count` treats a saturated estimate as
unknowable. As a result:

```console
$ bitbrew -p "***********" --charset lower --count     # 26^11 = 3,670,344,486,987,776
Warning: estimated 1,000,000,000,000,000 combinations. Use --force to proceed.
```

- `--count`, which the README describes as "instant" and as needing no `--force`, refuses,
  and the number in the warning is wrong.
- `--count --force` then *enumerates* a space of 3.7 × 10^15 words, which never finishes in
  practice.
- The public `estimate_count("*" * 11, 26)` returns `1000000000000000` with no indication
  that the value was clamped.
- A space of exactly 10^15 (`-p` with 15 `*` and `--charset digits`) is also misreported as
  unknowable, because of the `>=`.

The clamp protects nothing: Python integers have arbitrary precision, and the product is at
most one multiply per wildcard.

**Remediation:** delete `_MAX_ESTIMATE` and the clamp, and drop the saturation test from
`exact_output_count`. If huge numbers make the warning hard to read, format them in
scientific notation past some size, but keep the value exact.

**Tests:** check that `estimate_count("*" * 11, 26) == 26**11`, and that `--count` on that
pattern prints `26**11` and returns 0 without `--force`.

### M5. Newlines in the charset or pattern corrupt the output, and `--charset-file` alters input

**Where:** `load_charset_file` (`bitbrew.py:133-155`), `_resolve_options`, and the writer,
which joins words with `"\n"`.

The output format is one word per line, but nothing stops a word from containing a newline:

```console
$ printf 'ab\n\n' > cs.txt          # editor left a trailing blank line
$ bitbrew -p "x*" --charset-file cs.txt | od -c
x a \n x b \n x \n \n                # "x\n" became two lines: "x" and ""
```

Related defects in `load_charset_file`:
- The file is opened in text mode with universal newlines. Python has already turned `\r\n`
  into `\n` by the time `data.endswith("\r\n")` runs, so that branch (`151-152`) is dead code.
  It is also the uncovered line 152 in the coverage report.
- Because of the same translation, a lone `\r` in the file becomes `\n`. A file holding
  `a\rb` yields the charset `a\nb`, so the file is not used "verbatim" as documented.
- A UTF-8 BOM (`﻿`) becomes a charset character.

**Remediation:**
1. Open the file with `newline=""` so nothing is translated, and keep the existing
   `\r\n`/`\n` strip (it then becomes live). Use `encoding="utf-8-sig"` to drop a BOM.
2. In `_resolve_options`, reject `\n` and `\r` in the resolved charset and in pattern
   literals, with a message naming the culprit:
   `"--charset-file contains a newline; words would span lines"`.

**Tests:** cover a trailing blank line, CRLF, a lone `\r`, a BOM, and `-p $'a\nb*'`.

---

## Low

### L1. The README says an interrupted compressed stream is a valid archive

**Where:** `README.md:356`. The code comment at `bitbrew.py:1775-1777` gets it right.

The README says "A run interrupted mid-`--compress` leaves a valid archive of however much
it had finished". Tested three times with
`timeout -s INT 1 bitbrew -p '******' --force --compress > o.gz`, `gzip -t` reported
"format violated" twice and "length error" once. The SIGINT handler raises inside a write,
so the deflate stream is cut mid-block.

**Remediation:** at minimum, correct the README sentence to match the code comment and the
"Compressed output is incomplete" message. To actually leave a valid archive, install a
flag-only handler around `_stream_to_stdout`'s gzip path and poll it between chunks. The
`with GzipFile` exit then writes a clean trailer. Exit code 130 still marks the archive as a
prefix of the full list.

### L2. `--chunk-size` has no upper bound on the stdout path

**Where:** `_stream_to_stdout` (`bitbrew.py:1735`, `1769`). `_chunked_write` caps the block
only when `should_stop` is passed (`728`), and the stdout path passes `None`.

`bitbrew -p '*****' --force --chunk-size 20000000 > /dev/null` peaks at 795 MB RSS, compared
with 23 MB at the default. The `-o` path is capped at 10,000 by its poll interval.

**Remediation:** clamp every path to a fixed ceiling (`min(chunk_size, 100_000)`; larger
blocks buy nothing once the destination buffers them), or validate `--chunk-size` against a
documented maximum.

### L3. The exact-dedup warning ignores `--limit`

**Where:** `_dedup_stage` (`bitbrew.py:1490-1496`) sizes the warning from
`cfg.total_estimate`.

```console
$ bitbrew -p "*****" -p "a****" --limit 3 --force >/dev/null
Warning: deduplicating ~12,338,352 words holds them all in memory (roughly 1.1 GiB) ...
```

Only three words are ever held. **Remediation:** use `cfg.dedup_capacity`, which already
accounts for `--limit` and is what the Bloom path sizes from.

### L4. Ctrl-C during option resolution prints a traceback

**Where:** `main` (`bitbrew.py:1799-1804`) calls `_resolve_options`, which runs the ReDoS
probe, before `_interrupt_guard` is installed, and does not catch `KeyboardInterrupt`.
Interrupting a slow screen (M3) produces a full traceback.

**Remediation:** wrap the body of `main` in `except KeyboardInterrupt: print("\nInterrupted.",
file=sys.stderr); return 130`, or install the guard before resolving options.

### L5. Rough edges around `-o`

- `-o existing_dir` reports "already exists. Use --overwrite to replace". With `--overwrite`
  it then fails with `[Errno 21] Is a directory`. Detect a directory in `_resolve_options`
  and say so.
- `-o -` writes a file named `-` and does not mean stdout. Either treat `-` as stdout (the
  usual convention) or document that it does not.
- Placement creates a sidecar in the output's directory, so it needs write permission on the
  **directory**, not just the file. A writable file in a read-only directory fails where
  `>` redirection would succeed. Document this, or fall back to direct writes as in H1.

### L6. The build floor is wrong for the license field

**Where:** `pyproject.toml:2-3, 10`. `license = "MIT"` is a PEP 639 SPDX string, which
setuptools supports only from version 77. Building with setuptools 70.3.0 and
`--no-build-isolation` fails:

```text
configuration error: `project.license` must be valid exactly by one definition (2 matches found)
```

Isolated builds work only because pip fetches the latest setuptools. **Remediation:** change
the floor to `requires = ["setuptools>=77"]`. While editing the file, consider adding
`authors`, `classifiers` and `[project.urls]`, all of which the metadata currently lacks.

### L7. CI matrix and pinning

**Where:** `.github/workflows/ci.yml:20, 22-23, 69`.

- Python 3.10 reaches end of life in October 2026, and 3.14 is current but untested. Add
  `"3.14"` to the matrix, and decide whether to keep 3.10 and `requires-python = ">=3.10"`.
- Actions are pinned to moving tags (`actions/checkout@v4`). Pin them to commit SHAs, with
  the tag in a comment, to harden the supply chain.
- `pip install ruff mypy` is unpinned, so a new ruff release that adds a rule to a selected
  family (`B`, `SIM`, `UP`) can turn the lint job red on an unrelated change. Pin the
  versions in a constraints file or a `[dev]` extra.
- `mypy` checks only `bitbrew.py` (`pyproject.toml:39`). Adding `test_bitbrew.py` catches
  stale test helpers.

### L8. Wall-clock assertions and the absolute-time probe budget

**Where:** `test_bitbrew.py:213` and `958` (`< 0.05 s`), `1296` (`< 1.0 s`). The probe
budget is at `bitbrew.py:307` and `553`.

The tests assert wall-clock bounds, and they also run under coverage tracing, which slows
them about threefold. A loaded shared runner can fail them spuriously. The production probe
has the same property: it rejects a pattern once *cumulative absolute* time exceeds 50 ms.
The README (`README.md:191`) says it rejects a pattern "if the time grows exponentially", but
nothing measures growth. A GC pause or a scheduler stall during screening can reject a safe
filter at random.

**Remediation:** in the probe, compare times across rungs (for example, flag only when time
per rung keeps growing by more than 2× over three consecutive rungs, in addition to a hard
cap), so one stall cannot trip it. In tests, make the bounds generous (≥10× headroom) or
count work instead of time. For example, assert that `_check_regex_safety` visits each index
a bounded number of times.

---

## Informational

- **I1. Non-reproducible gzip output.** `GzipFile(fileobj=raw)` records the current mtime and
  `raw.name`, so the stdout path stores the filename `<stdout>` in the header
  (`bitbrew.py:1588`, `1768`). Pass `filename=""` and `mtime=0` so identical inputs produce
  byte-identical archives, which makes diffing and caching wordlists easier.
- **I2. Over-eager dedup for `?`.** `_needs_dedup` turns on deduplication, with its O(output)
  memory, for any single pattern containing `?`. Some cannot duplicate, such as `a?` and
  `?*`, because their word lengths differ. This is a possible optimisation, not a bug: a
  precise test is "two `?`, or a `?` adjacent to literals that are in the charset".
- **I3. Test organisation.** Several test classes are named after review rounds
  (`TestAuditFixes`, `TestReviewFindings`) rather than after behaviour. Moving their cases
  into the feature classes they exercise makes gaps easier to see. The single 2.7k-line test
  file could be split by area while staying stdlib-plus-pytest.
- **I4. Library docstrings.** `generate_wordlist` raises `ValueError` lazily, on the first
  `next()`, for a dangling backslash, but its docstring has no `Raises:` section.
  `estimate_count`'s docstring calls its result "capped", which hides M4.

## Verification notes

Every finding above was reproduced at `d8c91e6` in a Linux container (Python 3.13, running
as root, umask 022). All reproductions ran in a scratch directory. The device-node case used
a node made with `mknod` there, not the real `/dev/null`. No source files were changed while
reviewing.
