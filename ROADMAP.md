# Development roadmap

## Completed

- Make SIGINT interrupt generation, counting, writing, and a currently evaluating candidate.
- Refuse approximate deduplication whenever the 2 GiB ceiling cannot honor the requested
  false-positive rate.
- Run full pytest discovery in CI instead of naming one test module.
- Make structural ReDoS screening linear and distinguish quantifiers from escaped or
  character-class literals.
- Let `--count` ignore an existing `-o` path because count mode does not write output.
- Declare least-privilege permissions in the GitHub Actions workflow.
- Add coverage reporting to CI, gated at 93%.
- Place output create-only unless `--overwrite`, closing the gap between the startup
  guard and the write, and create the sidecar without reading the process umask.
- Flush stdout inside its own handlers, so a short run reports a failed write as exit 1
  and a closed pipe as a silent exit 0 instead of a shutdown traceback and exit 120.
- Stop reporting sidecar-name exhaustion as an existing output file.
- Count range brace quantifiers as repetitions when screening `--filter` regexes.
- Expand patterns entirely in C by folding literal runs into the wildcard product.
- Honour `--chunk-size` on the stdout stream, keeping a terminal line-at-a-time.
- Seed the ReDoS timing probe with characters the pattern matches rather than the
  characters of its source, so class-based alternation such as `(\s|\s)+$` is screened.
- Warn when a `--charset` part looks like a mistyped preset instead of silently using it
  as literal characters.
- Show the progress bar for redirected stdout runs, and keep it off a terminal and out of
  a redirected stderr.
- Write character devices and FIFOs in place, follow symlinks to their target, keep an
  overwritten file's permission bits, and refuse other file types, so `--overwrite` never
  replaces the entry `-o` names (H1).
- Refuse charset and pattern text that is not valid UTF-8, or that stdout's encoding cannot
  represent, before writing, and remove the sidecar on any failure (M1).
- Refuse line breaks in the charset and patterns, and read `--charset-file` untranslated and
  without its byte order mark (M5).
- Start the ReDoS probe ladder at one character so screening cannot stall on its first
  search (M3).
- Probe ReDoS candidates behind the literal prefix they require (M2).
- Exit 130 instead of a traceback on Ctrl-C during option resolution (L4).
- Keep word-count estimates exact past 10**15, so `--count` and `estimate_count()` answer
  any pattern size (M4).
- Correct the README: an interrupted `--compress` stream is not a valid archive (L1).
- Cap `--chunk-size` at 100,000 words on every output path (L2).
- Size the exact-dedup memory warning from `--limit` (L3).
- Require setuptools 77, the first to accept the SPDX license string (L6).
- Test Python 3.11-3.14 and drop 3.10, which reaches end of life in October 2026 (L7).

## Next

Phase 3 review findings still open, detailed with repros and fixes in
[docs/REMEDIATION_REPORT.md](docs/REMEDIATION_REPORT.md):

- L5: Say when `-o` names a directory instead of suggesting `--overwrite`, and decide
  whether `-o -` means stdout.
- L7: Pin GitHub Actions to commit SHAs and the lint tools to versions, and type-check the
  tests.
- L8: Replace wall-clock assertions in tests with work counts, and make the timing probe
  measure growth rather than an absolute budget.
- I1-I4: Reproducible gzip headers, narrower dedup for `?`, test classes grouped by
  behaviour, and complete library docstrings.
