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

## Next

Nothing outstanding from the audit. New items land here as they are found.
