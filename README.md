# 🍺 bitbrew

> **Pattern-based wordlist generator** — brew your wordlists one wildcard at a time.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-pytest-green.svg)](https://docs.pytest.org/)

`bitbrew` is a zero-dependency CLI tool and Python library that generates wordlists by expanding patterns with wildcard substitution. Think of it as a wordlist brewery — you define the recipe (pattern + charset), and bitbrew ferments every combination.

---

## ✨ Features

- 🔣 **Wildcard patterns** — `*` for exactly one char, `?` for zero-or-one char, `\\` to escape either
- 🔡 **Flexible charsets** — built-in presets, comma-combined, or raw custom strings
- 📁 **Streaming output** — write to stdout, a file, or gzip-compressed output
- 🔍 **Filtering** — by minimum/maximum length and/or regex (screened for catastrophic backtracking)
- 🔂 **Multiple patterns** — supply `-p` multiple times; results are deduplicated by default
- 🔢 **Count mode** — preview how many words will be generated, answered instantly from arithmetic
- 📊 **Progress bar** — optional `tqdm` integration for large runs
- 🛡️ **Safety rails** — `--force` for >10 M combinations, `--overwrite` for existing files, atomic writes so a failed run never leaves a truncated wordlist
- ✂️ **Sampling** — `--limit N` takes the first N words of an arbitrarily large space
- 🐍 **Library API** — use `generate_wordlist()` directly in your own code

---

## 📦 Installation

Requires **Python 3.10+**.

```bash
# Clone the repository
git clone https://github.com/sparktron/bitbrew.git
cd bitbrew

# Install in development mode (no extra dependencies)
pip install -e .

# With optional progress bar (tqdm)
pip install -e ".[progress]"

# With dev dependencies (pytest)
pip install -e ".[dev]"
```

After installation the `bitbrew` command is on your `PATH`:

```bash
bitbrew -p "***" --charset digits
```

> **Zero-dependency mode:** `bitbrew.py` can be run directly as a standalone script — no install required, no third-party packages needed.

---

## 🚀 Quick start

```bash
# All 3-letter lowercase words
bitbrew -p "***"

# "pass0" through "pass9"
bitbrew -p "pass*" --charset digits

# Lowercase + digits mix
bitbrew -p "a**" --charset "lower,digits"

# Custom characters only
bitbrew -p "**" --charset "abc123"
```

---

## 📖 Usage

### Pattern syntax

| Wildcard | Meaning |
|----------|---------|
| `*` | **Exactly one** character from the active charset |
| `?` | **Zero or one** character — generates two variants per position |
| `\\` | Escapes the next character, making it literal |

Literal characters are preserved as-is. Multiple wildcards produce the full Cartesian product.

```bash
# ? generates optional positions: "ab" and "axb"
bitbrew -p "a?b" --charset "x"

# \\ makes a wildcard literal: emits the single word "pw*"
bitbrew -p 'pw\\*' --charset digits
```

---

### Charsets

| Preset | Characters |
|--------|------------|
| `lower` *(default)* | `a–z` (26 chars) |
| `upper` | `A–Z` (26 chars) |
| `digits` | `0–9` (10 chars) |
| `symbols` | `!@#$%^&*` (8 chars) |
| `all` | All of the above combined |

**Combine presets** with commas:

```bash
bitbrew -p "***" --charset "lower,digits,symbols"
```

**Or pass a raw string** — any characters not matching a preset name are used directly:

```bash
bitbrew -p "**" --charset "aeiou0123"
```

Duplicate characters in the resolved charset are automatically removed.

**Commas and spaces** cannot be expressed through `--charset`, since it splits on commas
and trims whitespace. Use `--charset-file` to supply a charset verbatim — every character
in the file is used as-is, apart from a single trailing newline:

```bash
printf 'abc, ' > charset.txt
bitbrew -p "**" --charset-file charset.txt
```

---

### Output options

```bash
# Write to a file
bitbrew -p "***" --charset digits -o words.txt

# Gzip-compressed output (auto-detected from .gz extension)
bitbrew -p "***" --charset digits -o words.txt.gz

# Force compression regardless of extension
bitbrew -p "***" --charset digits -o words.txt --compress

# Compressed straight to a pipe (refused if stdout is a terminal)
bitbrew -p "***" --charset digits --compress | gunzip | wc -l

# Overwrite an existing file
bitbrew -p "***" --charset digits -o words.txt --overwrite
```

---

### Filtering

Length bounds are inclusive and must be zero or greater.

```bash
# Keep only words between 4 and 5 characters long
bitbrew -p "a?b?c" --charset digits --min-len 4 --max-len 5

# Keep only words matching a regex (Python re syntax)
bitbrew -p "***" --charset "lower,digits" --filter "^a.*9$"
```

#### ReDoS screening

A `--filter` regex runs against every generated word, so one that backtracks
catastrophically turns a short run into an unbounded hang. bitbrew screens each filter in
two ways before generating anything:

1. **Structural check** — rejects nested quantifiers such as `(a+)+` or `(\d+)+`.
2. **Timing probe** — matches the regex against a short ladder of adversarial inputs built
   from the pattern's own alphabet and rejects it if the time grows exponentially. This
   catches the overlapping-alternation family, like `(a|a)+$` and `(a|b|ab)*$`, that no
   structural check sees.

The probe costs well under a millisecond for a normal filter and gives up as soon as its
own time budget is spent, so screening a pathological pattern is bounded too.

```bash
$ bitbrew -p "***" --filter "(a|a)+$"
Error: unsafe regex '(a|a)+$': matching took 176 ms on a 20-character input, which
indicates catastrophic backtracking (ReDoS). Use --allow-unsafe-regex to run it anyway.
```

> ⚠️ Screening is a **heuristic, not a guarantee**. It can miss a pathological pattern
> whose trigger does not resemble its own literals, and the structural check still rejects
> a few safe patterns such as `(ab+c)+`. Override it with `--allow-unsafe-regex` when you
> know a filter is fine — and do not treat it as a security boundary for regexes that come
> from somewhere you do not trust.

---

### Multiple patterns

Supply `-p` more than once. Results across all patterns are automatically **deduplicated** (first occurrence wins):

```bash
bitbrew -p "admin*" -p "root*" --charset digits -o wordlist.txt
```

Deduplication is the one part of the pipeline that is not constant-memory — it has to
remember every word it has emitted (roughly 100 bytes each). bitbrew therefore only
deduplicates when duplicates are actually **possible**: more than one `-p`, or a single
pattern containing `?`. A plain `bitbrew -p "******"` run skips it entirely and streams in
constant memory. When dedup is unavoidable and the run is large, bitbrew warns you and
you can stream without it:

```bash
bitbrew -p "admin*" -p "root*" --charset all --force --no-dedup -o wordlist.txt
```

Or keep deduplicating in **bounded memory** with `--dedup-approx`, which uses a Bloom
filter sized from the estimated output:

```bash
bitbrew -p "admin*" -p "root*" --charset all --force --dedup-approx -o wordlist.txt
```

> ⚠️ Approximate deduplication can drop a small, quantified fraction of **valid** words —
> that is the trade for constant memory. The default rate is one in a million; tune it with
> `--dedup-error`. bitbrew prints the filter's size and its expected error rate before
> starting, and sizes it for the sample rather than the whole space when `--limit` bounds
> what it can see. It never keeps a duplicate, and never drops a word it has not seen; the only
> error is dropping a word it wrongly believes it has seen.

---

### Count mode

Preview the number of words a pattern will generate — no output written:

```bash
bitbrew -p "****" --charset lower --count
# 456976
```

Count mode respects `--min-len`, `--max-len`, and `--filter`.

When nothing can drop a word — no filters, no deduplication — the answer is pure
arithmetic, so counting an enormous space is instant and needs no `--force`:

```bash
$ time bitbrew -p "*******" --charset lower --count
8031810176
real    0m0.05s
```

With filters in play the count is exact rather than estimated, which means bitbrew has to
generate the words to count them.

---

### Sampling with `--limit`

`--limit N` stops after N words, which makes a huge pattern space explorable without
generating it:

```bash
# First 5 of 8 billion, instantly
bitbrew -p "*******" --charset lower --limit 5
```

When nothing can discard a candidate, `--limit` bounds the work as well as the output, so
sampling needs no `--force`. Add a `--filter`, `--min-len`/`--max-len`, or deduplication
and that stops being true — a filter matching nothing walks the entire space regardless of
the limit — so the guard goes back to asking about the full estimate:

```bash
# Refused: this could examine all 8 billion candidates to emit one word
bitbrew -p "*******" --charset lower --filter "^Z$" --limit 1
```

---

### Large wordlists

Generating more than **10 million** combinations requires `--force`:

```bash
# 26^5 = 11,881,376 words, about 68 MB on disk
bitbrew -p "*****" --charset lower --force -o big.txt
```

Check the size before you commit to it — `--count` and the `--force` warning both tell you
what you are about to generate. Each extra `*` multiplies the output by the charset size:
`-p "******"` over `lower` is 308 million words and roughly 2 GB of text.

Control the streaming buffer size with `--chunk-size` (default: 10,000 words):

```bash
bitbrew -p "*****" --charset lower --force -o big.txt --chunk-size 50000
```

**Optional progress bar** — install `tqdm` and bitbrew will display a live progress counter automatically when writing to a file:

```bash
pip install tqdm
bitbrew -p "*****" --charset lower --force -o big.txt
# Generating: 100%|████████████| 11.9M/11.9M [00:04<00:00, 2.54Mwords/s]
```

When `--min-len`, `--max-len`, `--filter`, or deduplication are in play the final count
is not knowable up front, so bitbrew shows a plain counter instead of a percentage bar
that could never reach 100%.

**Interrupted?** Pressing `Ctrl+C` during file output stops generation promptly, removes
the partial file, and exits with code `130`.

**Atomic output:** bitbrew builds into a `<output>.part` sidecar and renames it into place
only once the run completes. An interrupted or failed run — a full disk, a permissions
error — leaves no truncated wordlist at your output path.

---

### Safety flags

| Flag | Purpose |
|------|---------|
| `--force` | Required when estimated output exceeds 10 M words |
| `--overwrite` | Required to overwrite an existing output file |

---

## 🐍 Library usage

```python
from bitbrew import generate_wordlist

# Yields: pass00, pass01, ..., pass99
for word in generate_wordlist("pass**", "digits"):
    print(word)
```

`generate_wordlist` is a **lazy generator** — it never materialises the full list in memory, making it safe for enormous pattern spaces. It performs no deduplication; that is a CLI concern, described under [Multiple patterns](#multiple-patterns) above.

You can also access the lower-level helpers directly:

```python
from bitbrew import resolve_charset, estimate_count

charset = resolve_charset("lower,digits")       # "abcde...xyz0123456789"
count   = estimate_count("****", len(charset))  # 1,679,616
```

---

## 🔧 CLI reference

```
usage: bitbrew [-h] [--version] -p PATTERN [-o OUTPUT] [--charset CHARSET]
               [--charset-file CHARSET_FILE] [--min-len MIN_LEN]
               [--max-len MAX_LEN] [--filter REGEX_FILTER] [--limit LIMIT]
               [--count] [--compress] [--chunk-size CHUNK_SIZE] [--force]
               [--overwrite] [--allow-unsafe-regex] [--no-dedup]
               [--dedup-approx] [--dedup-error DEDUP_ERROR]
```

| Flag | Description |
|------|-------------|
| `-p, --pattern` | Pattern to expand — repeatable, results are deduplicated |
| `-o, --output` | Output file path (default: stdout) |
| `--charset` | Preset name(s) or raw char string (default: `lower`) |
| `--charset-file` | Read the charset verbatim from a file |
| `--min-len` | Minimum word length (inclusive) |
| `--max-len` | Maximum word length (inclusive) |
| `--filter` | Python regex; only matching words are kept |
| `--limit` | Stop after N words |
| `--count` | Print word count only — no words emitted |
| `--compress` | Write gzip-compressed output |
| `--chunk-size` | Words per streaming chunk (default: `10000`) |
| `--force` | Allow >10 M combinations |
| `--overwrite` | Overwrite an existing output file |
| `--allow-unsafe-regex` | Skip `--filter` safety screening |
| `--no-dedup` | Stream without deduplicating (constant memory, duplicates kept) |
| `--dedup-approx` | Deduplicate in bounded memory via a Bloom filter |
| `--dedup-error` | Target false-positive rate for `--dedup-approx` (default: `1e-6`) |
| `--version` | Print the version and exit |

---

## 🧪 Running tests

```bash
pip install -e ".[dev,progress]"
python -m pytest test_bitbrew.py -v
```

Linting uses [ruff](https://docs.astral.sh/ruff/) and type-checking uses
[mypy](https://mypy-lang.org/) in `--strict` mode. CI runs both alongside the test matrix
on Python 3.10–3.13:

```bash
ruff check .
mypy
```

The test suite covers:
- Charset resolution and deduplication
- Pattern expansion (including edge cases with `?` and mixed wildcards)
- All CLI flags and error paths
- Streaming / chunked writes (text and gzip)
- Interrupt handling and partial file cleanup, including a real `SIGINT` delivered to a
  live generation run
- Atomic output: no truncated file is left behind on error or interrupt
- Deduplication policy and `--no-dedup`
- ReDoS screening: both layers, plus the bound on screening's own cost
- Compressed output to a file and to a pipe
- The installed `bitbrew` console script
- Pattern escapes, `--charset-file`, `--limit`, and analytic counting
- The Bloom filter's *measured* false-positive rate against its predicted one
- Large-pattern stress tests

---

## 📄 License

[MIT](LICENSE) — brew freely.
