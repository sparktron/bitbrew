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

## Next

Nothing outstanding from the audit. New items land here as they are found.
