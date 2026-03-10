# bitbrew

A Python CLI tool that generates wordlists by expanding patterns with wildcard substitution.

## Installation

Requires Python 3.10+.

```bash
# Clone the repository
git clone https://github.com/sparktron/bitbrew.git
cd bitbrew

# Install optional dependencies (progress bar)
pip install tqdm

# Install test dependencies
pip install pytest
```

No other installation is needed — `bitbrew.py` is a standalone script with no required third-party dependencies.

## Usage

### Basic examples

```bash
# Generate all 3-letter lowercase words
python bitbrew.py -p "***"

# Generate words like "pass0" through "pass9"
python bitbrew.py -p "pass*" --charset digits

# Multiple charsets: lowercase + digits
python bitbrew.py -p "a**" --charset "lower,digits"

# Custom character set
python bitbrew.py -p "**" --charset "abc123"
```

### Pattern syntax

| Wildcard | Meaning |
|----------|---------|
| `*` | Exactly one character from the active charset |
| `?` | Zero or one character (produces two variants per position) |

Literal characters are preserved as-is. Multiple wildcards produce the full cartesian product.

```bash
# ? generates optional positions: "ab" and "axb"
python bitbrew.py -p "a?b" --charset "x"
```

### Charsets

| Name | Characters |
|------|------------|
| `lower` (default) | a–z |
| `upper` | A–Z |
| `digits` | 0–9 |
| `symbols` | `!@#$%^&*` |
| `all` | All of the above combined |

Combine presets with commas: `--charset "lower,digits,symbols"`

Or pass a raw string: `--charset "aeiou0123"`

### Output options

```bash
# Write to a file
python bitbrew.py -p "***" --charset digits -o words.txt

# Compressed output (auto-detected from .gz extension)
python bitbrew.py -p "***" --charset digits -o words.txt.gz

# Or use --compress flag explicitly
python bitbrew.py -p "***" --charset digits -o words.txt --compress
```

### Filtering

```bash
# Length filters
python bitbrew.py -p "a?b?c" --charset digits --min-len 4 --max-len 5

# Regex filter: only words starting with "a" and ending with "9"
python bitbrew.py -p "***" --charset "lower,digits" --filter "^a.*9$"
```

### Multiple patterns

Use `-p` multiple times. Results are automatically deduplicated:

```bash
python bitbrew.py -p "admin*" -p "root*" --charset digits -o wordlist.txt
```

### Count mode

Preview how many words a pattern generates without outputting them:

```bash
python bitbrew.py -p "****" --charset lower --count
# Output: 456976
```

### Large wordlists

Generating more than 10 million combinations requires `--force`:

```bash
python bitbrew.py -p "******" --charset lower --force -o big.txt
```

Use `--chunk-size` to control the streaming buffer (default: 10,000 words):

```bash
python bitbrew.py -p "******" --charset lower --force -o big.txt --chunk-size 50000
```

### Safety flags

| Flag | Purpose |
|------|---------|
| `--force` | Required when estimated output exceeds 10M words |
| `--overwrite` | Required to overwrite an existing output file |

Pressing Ctrl+C during file output deletes the partial file automatically.

### Library usage

```python
from bitbrew import generate_wordlist

for word in generate_wordlist("pass**", "digits"):
    print(word)  # pass00, pass01, ..., pass99
```

## CLI reference

```
usage: bitbrew.py [-h] -p PATTERN [-o OUTPUT] [--charset CHARSET]
                             [--min-len MIN_LEN] [--max-len MAX_LEN]
                             [--filter REGEX] [--count] [--compress]
                             [--chunk-size CHUNK_SIZE] [--force] [--overwrite]
```

| Flag | Description |
|------|-------------|
| `-p, --pattern` | Pattern to expand (repeatable) |
| `-o, --output` | Output file path (default: stdout) |
| `--charset` | Charset preset or raw string (default: `lower`) |
| `--min-len` | Minimum word length |
| `--max-len` | Maximum word length |
| `--filter` | Regex filter on results |
| `--count` | Print word count only |
| `--compress` | Write gzip-compressed output |
| `--chunk-size` | Words per streaming chunk (default: 10000) |
| `--force` | Allow >10M combinations |
| `--overwrite` | Overwrite existing output file |

## Running tests

```bash
python -m pytest test_bitbrew.py -v
```
