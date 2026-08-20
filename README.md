# 🍺 bitbrew

> **Pattern-based wordlist generator** — brew your wordlists one wildcard at a time.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-pytest-green.svg)](https://docs.pytest.org/)

`bitbrew` is a zero-dependency CLI tool and Python library that generates wordlists by expanding patterns with wildcard substitution. Think of it as a wordlist brewery — you define the recipe (pattern + charset), and bitbrew ferments every combination.

---

## ✨ Features

- 🔣 **Wildcard patterns** — `*` for exactly one char, `?` for zero-or-one char
- 🔡 **Flexible charsets** — built-in presets, comma-combined, or raw custom strings
- 📁 **Streaming output** — write to stdout, a file, or gzip-compressed output
- 🔍 **Filtering** — by minimum/maximum length and/or regex (with partial ReDoS screening)
- 🔂 **Multiple patterns** — supply `-p` multiple times; results are deduplicated by default
- 🔢 **Count mode** — preview how many words will be generated without outputting them
- 📊 **Progress bar** — optional `tqdm` integration for large runs
- 🛡️ **Safety rails** — `--force` for >10 M combinations, `--overwrite` for existing files, atomic writes so a failed run never leaves a truncated wordlist
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

Literal characters are preserved as-is. Multiple wildcards produce the full Cartesian product.

```bash
# ? generates optional positions: "ab" and "axb"
bitbrew -p "a?b" --charset "x"
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

---

### Output options

```bash
# Write to a file
bitbrew -p "***" --charset digits -o words.txt

# Gzip-compressed output (auto-detected from .gz extension)
bitbrew -p "***" --charset digits -o words.txt.gz

# Force compression regardless of extension
bitbrew -p "***" --charset digits -o words.txt --compress

# Overwrite an existing file
bitbrew -p "***" --charset digits -o words.txt --overwrite
```

---

### Filtering

```bash
# Keep only words between 4 and 5 characters long
bitbrew -p "a?b?c" --charset digits --min-len 4 --max-len 5

# Keep only words matching a regex (Python re syntax)
bitbrew -p "***" --charset "lower,digits" --filter "^a.*9$"
```

> ⚠️ **Partial ReDoS screening:** bitbrew rejects `--filter` regexes containing nested
> quantifiers (e.g. `(a+)+`), one common source of catastrophic backtracking. This is a
> heuristic, **not a guarantee**: it does not catch overlapping alternation such as
> `(a|a)+`, and it rejects some patterns that are actually safe, like `(ab+c)+`. Do not
> rely on it as a security boundary for regexes from an untrusted source.

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

---

### Count mode

Preview the number of words a pattern will generate — no output written:

```bash
bitbrew -p "****" --charset lower --count
# 456976
```

Count mode respects `--min-len`, `--max-len`, and `--filter`.

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

The progress total is the *estimated* expansion, so it runs ahead of the real count when
`--min-len`, `--max-len`, or `--filter` discard words.

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
usage: bitbrew [-h] -p PATTERN [-o OUTPUT] [--charset CHARSET]
               [--min-len MIN_LEN] [--max-len MAX_LEN]
               [--filter REGEX_FILTER] [--count] [--compress]
               [--chunk-size CHUNK_SIZE] [--force] [--overwrite]
               [--no-dedup]
```

| Flag | Description |
|------|-------------|
| `-p, --pattern` | Pattern to expand — repeatable, results are deduplicated |
| `-o, --output` | Output file path (default: stdout) |
| `--charset` | Preset name(s) or raw char string (default: `lower`) |
| `--min-len` | Minimum word length (inclusive) |
| `--max-len` | Maximum word length (inclusive) |
| `--filter` | Python regex; only matching words are kept |
| `--count` | Print word count only — no words emitted |
| `--compress` | Write gzip-compressed output |
| `--chunk-size` | Words per streaming chunk (default: `10000`) |
| `--force` | Allow >10 M combinations |
| `--overwrite` | Overwrite an existing output file |
| `--no-dedup` | Stream without deduplicating (constant memory, duplicates kept) |

---

## 🧪 Running tests

```bash
python -m pytest test_bitbrew.py -v
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
- ReDoS screening
- Large-pattern stress tests

---

## 📄 License

[MIT](https://opensource.org/licenses/MIT) — brew freely.
