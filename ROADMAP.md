# Development roadmap

## Completed

- Make SIGINT interrupt generation, counting, writing, and a currently evaluating candidate.
- Refuse approximate deduplication whenever the 2 GiB ceiling cannot honor the requested
  false-positive rate.
- Run full pytest discovery in CI instead of naming one test module.
- Make structural ReDoS screening linear and distinguish quantifiers from escaped or
  character-class literals.

## Next

- Let `--count` ignore an existing `-o` path because count mode does not write output.
- Declare least-privilege permissions in the GitHub Actions workflow.
- Add coverage reporting to CI.
- Review atomic replacement and umask handling for concurrent library callers.
