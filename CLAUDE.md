# CLAUDE.md

Orientation for AI/coding sessions on **bitbrew**, a pattern-based wordlist
generator. This is the contributor's-eye view; user-facing docs live in
`README.md`. Trust the code over this file where they disagree.

## Layout

Everything is in two files. `bitbrew.py` is the module, the library, and the
CLI; `test_bitbrew.py` is the whole suite. `setup.py` is a shim — packaging
metadata lives in `pyproject.toml`. There is no `src/` layout and no package
directory; `py-modules = ["bitbrew"]`.

## Working commands

```bash
pip install -e ".[dev,progress]"   # dev = pytest, ruff, mypy; progress = tqdm
python -m pytest test_bitbrew.py   # full suite, ~3s
ruff check .                       # lint + import order (autofix: --fix)
mypy                               # strict mode, config in pyproject.toml
```

CI runs all three across Python 3.10–3.13 and smoke-tests the installed
`bitbrew` console script. Keep ruff and `mypy --strict` clean; both are
merge gates. `bitbrew.py` must stay runnable as a plain script (`python
bitbrew.py ...`) with no install and no third-party import at module load —
`tqdm` is imported lazily and is optional.

## Pattern grammar

Parsed once in `_parse_pattern`, which every consumer (`_expand_pattern`,
`estimate_count`, `_needs_dedup`, the no-wildcard warning) shares — they must
agree on what a wildcard is, so add new syntax there, not in each caller.

- `*` — exactly one character from the active charset
- `?` — zero or one character (emits both variants)
- `\` — escapes the next character; `\*` is a literal asterisk, `\\` a literal
  backslash. A trailing `\` is an error.

## Charsets

`resolve_charset` splits `--charset` on commas, resolves preset names
(`lower`, `upper`, `digits`, `symbols`, `all`), treats anything else as raw
characters, and de-duplicates preserving order. Because it splits on commas
and strips whitespace, a comma or a lone space **cannot** be expressed this
way — that is what `--charset-file` (verbatim, one trailing newline trimmed)
is for. `--charset` and `--charset-file` are mutually exclusive.

## Design decisions and their deliberate limits

**Streaming is the core invariant.** The pipeline is a chain of generators;
nothing materialises the full wordlist. The one stage that costs memory is
deduplication, so it runs only when duplicates are actually possible
(`_needs_dedup`: more than one `-p`, or a single pattern containing `?`).
`--no-dedup` opts out; `--dedup-approx` swaps exact dedup for a Bloom filter
with bounded memory.

**Bloom filter (`_BloomFilter`, `--dedup-approx`).** Approximate: a false
positive drops a *valid* word, so it is never the default and always prints
its size and expected error rate first. It never keeps a duplicate and never
drops an unseen word. Uses the built-in `hash()` (fast, per-process seed) —
fine because the filter is never persisted or shared. `dedup_capacity` sizes
it for the *sample* when `--limit` bounds what it can see, but only when no
filter/length bound sits downstream to still expose the whole space. The
measured error rate is asserted against the predicted one in the tests; keep
that test if you touch the hashing.

**ReDoS screening is a heuristic, not a guarantee.** Two layers screen
`--filter`: a structural check (`_check_regex_safety`, nested quantifiers,
ignores escaped characters) and a timing probe (`_probe_regex_blowup`, times
the regex against adversarial inputs built from its own alphabet). A true
in-process time budget is impossible — Python signal handlers only run
between bytecode ops and cannot interrupt a regex backtracking inside C — so
the probe is the best available and can still be fooled. `--allow-unsafe-regex`
overrides both. Do not describe this as a security boundary.

**`--count` is analytic when it can be.** With no filter, no dedup, and a
non-saturated estimate, the count comes from `estimate_count` in closed form
(instant, no `--force` needed). `_RunConfig.exact_output_count` /
`effective_scale` / `dedup_capacity` centralise the "can we know the size
without generating?" logic — reuse them rather than re-deriving it.

**File output is atomic.** `_write_to_file` builds into a uniquely-named
`mkstemp` sidecar in the destination directory and places it on success:
`os.replace` with `--overwrite`, else `_place_output`'s create-only hard link
that fails atomically if the path was taken (closing a TOCTOU against the
resolve-time guard). No failure or interrupt leaves a truncated file at the
output path. `mkstemp` is 0600, so the finished file is chmod'd to
`0o666 & ~umask` — the mode a plain `open()` would give.

**Interrupts.** `_chunked_write` polls a `should_stop` predicate so SIGINT
stops generation promptly (exit 130) and removes the sidecar. Signal handling
lives only in the file-output path; do not move it to import time (it breaks
library use off the main thread).

## Architecture

`main()` is a thin orchestrator. `_resolve_options` validates argv into a
frozen `_RunConfig`, raising `_CliError` for user errors (one message +
exit 1, instead of print-and-return scattered through the code).
`_build_pipeline` composes generate → dedup → filter → limit. `_write_to_file`
/ `_write_to_stdout` consume it. Validation belongs in `_resolve_options`;
pipeline shape in `_build_pipeline`; nothing else should grow a branch.

## Conventions

Google-style docstrings with Args/Returns/Yields/Raises on every function,
including private ones. Full type annotations (`mypy --strict`). `master` uses
merge commits titled `PR #N <description>`. Add a regression test for every
bug fix and reproduce the bug by measurement before fixing it.
