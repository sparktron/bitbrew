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

## Next

Phase 3 review findings, detailed with repros and fixes in
[docs/REMEDIATION_REPORT.md](docs/REMEDIATION_REPORT.md):

- H1: Stop `--overwrite` from replacing device nodes, FIFOs and symlinks, and keep an
  overwritten file's mode.
- M1: Reject non-UTF-8 input up front, and remove the sidecar on any failure.
- M2: Probe ReDoS candidates behind the pattern's required literal prefix.
- M3: Bound the timing probe's own cost by starting the ladder short.
- M4: Drop the 10^15 estimate clamp so `--count` and `estimate_count()` stay exact.
- M5: Reject newlines in the charset and patterns, and read `--charset-file` untranslated.
- L1-L8: Correct the interrupted-gzip README claim, cap `--chunk-size`, size the dedup
  warning from `--limit`, handle Ctrl-C during screening, tidy the `-o` edge cases, raise
  the setuptools floor to 77, refresh the CI matrix and pins, and de-flake the timing checks.
