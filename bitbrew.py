#!/usr/bin/env python3
"""bitbrew — pattern-based wordlist generator CLI tool.

Generates wordlists from patterns using wildcard substitution with support for
multiple charsets, filtering, streaming output, and compression.

Pattern syntax:
    * — matches exactly one character from the active charset
    ? — optional: matches zero or one character (produces two variants)
    \\ — escapes the next character, so "\\*" is a literal asterisk
    Literal characters are preserved as-is

Usage as a library:
    from bitbrew import generate_wordlist
    for word in generate_wordlist("pass*", "digits"):
        print(word)
"""

import argparse
import contextlib
import dataclasses
import gzip
import io
import itertools
import math
import os
import re
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Iterable, Iterator
from typing import Optional, Protocol

__version__ = "0.1.0"


class _ProgressBar(Protocol):
    """The slice of the tqdm API bitbrew uses."""

    def update(self, n: int) -> None:
        """Advance the bar by n items."""

    def close(self) -> None:
        """Finalise the bar. Must tolerate being called twice."""


class _CliError(Exception):
    """A user-facing argument or environment problem.

    Raised by option resolution and turned into a message plus exit code 1 by
    main(), so validation code does not repeat print-and-return at every check.
    """

CHARSETS = {
    "lower": "abcdefghijklmnopqrstuvwxyz",
    "upper": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "digits": "0123456789",
    "symbols": "!@#$%^&*",
}
CHARSETS["all"] = CHARSETS["lower"] + CHARSETS["upper"] + CHARSETS["digits"] + CHARSETS["symbols"]


def resolve_charset(spec: str) -> str:
    """Resolve a charset specification to a character string.

    Args:
        spec: A preset name (lower, upper, digits, symbols, all),
              a comma-separated combination of preset names,
              or a raw character string.

    Returns:
        The resolved character set as a string.
    """
    parts = [p.strip() for p in spec.split(",")]
    chars = ""
    for part in parts:
        if part in CHARSETS:
            chars += CHARSETS[part]
        else:
            # Treat as raw characters
            chars += part
    return _dedupe_chars(chars)


def _dedupe_chars(chars: str) -> str:
    """Remove duplicate characters, preserving first-appearance order.

    Args:
        chars: Raw character sequence.

    Returns:
        The deduplicated character string.
    """
    seen: set[str] = set()
    result = []
    for char in chars:
        if char not in seen:
            seen.add(char)
            result.append(char)
    return "".join(result)


def load_charset_file(path: str) -> str:
    """Read a charset verbatim from a file.

    Unlike --charset, nothing is split or stripped, so commas and spaces can be
    part of the charset. A single trailing newline is ignored, since almost
    every editor adds one.

    Args:
        path: File to read.

    Returns:
        The deduplicated character string.

    Raises:
        OSError: If the file cannot be read.
    """
    with open(path, encoding="utf-8") as handle:
        data = handle.read()
    if data.endswith("\r\n"):
        data = data[:-2]
    elif data.endswith("\n"):
        data = data[:-1]
    return _dedupe_chars(data)


_MAX_ESTIMATE = 10**15  # cap to prevent unbounded memory/time
_DEDUP_WARN_THRESHOLD = 1_000_000  # warn above this when dedup is in play
_BYTES_PER_WORD = 100  # measured cost of holding one short word in a set


def estimate_count(pattern: str, charset_len: int) -> int:
    """Estimate the total number of words a pattern will generate.

    Args:
        pattern: The pattern string with * and ? wildcards.
        charset_len: Number of characters in the active charset.

    Returns:
        Estimated word count (capped at _MAX_ESTIMATE).

    Raises:
        ValueError: If the pattern ends with a dangling backslash.
    """
    _, kinds = _parse_pattern(pattern)
    count = 1
    for kind in kinds:
        # "?" has one extra option: matching nothing at all.
        count *= charset_len if kind == "*" else charset_len + 1
        if count > _MAX_ESTIMATE:
            return _MAX_ESTIMATE
    return count


def generate_wordlist(pattern: str, charset: str = "lower") -> Generator[str, None, None]:
    """Generate words from a pattern by substituting wildcards.

    Args:
        pattern: Pattern with * (one char) and ? (zero or one char) wildcards.
        charset: A charset preset name or raw character string.

    Yields:
        Generated words.
    """
    yield from _expand_pattern(pattern, resolve_charset(charset))


def _parse_pattern(pattern: str) -> tuple[list[str | int], list[str]]:
    """Split a pattern into literal runs and wildcard slots.

    A backslash escapes the following character, so "\\*" is a literal asterisk
    rather than a wildcard. Consecutive literals are merged into one segment so
    the expansion loop does less work per generated word.

    Args:
        pattern: The pattern string.

    Returns:
        (segments, kinds). segments mixes literal strings with integer indices
        into kinds; kinds[i] is "*" or "?" for the i-th wildcard.

    Raises:
        ValueError: If the pattern ends with a dangling backslash.
    """
    segments: list[str | int] = []
    kinds: list[str] = []
    literal: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 >= len(pattern):
                raise ValueError(
                    "pattern ends with a dangling backslash; "
                    "write '\\\\' for a literal backslash"
                )
            literal.append(pattern[index + 1])
            index += 2
            continue
        if char in "*?":
            if literal:
                segments.append("".join(literal))
                literal.clear()
            segments.append(len(kinds))
            kinds.append(char)
            index += 1
            continue
        literal.append(char)
        index += 1
    if literal:
        segments.append("".join(literal))
    return segments, kinds


def _expand_pattern(pattern: str, chars: str) -> Generator[str, None, None]:
    """Expand a pattern into all matching words.

    Args:
        pattern: The pattern string.
        chars: The resolved charset characters.

    Yields:
        Generated words.

    Raises:
        ValueError: If the pattern ends with a dangling backslash.
    """
    segments, kinds = _parse_pattern(pattern)

    if not kinds:
        # No wildcards — yield the pattern's literal text, escapes resolved.
        yield "".join(seg for seg in segments if isinstance(seg, str))
        return

    # "?" also offers the empty string, which is how it matches zero characters.
    wildcards = [list(chars) if kind == "*" else ["", *chars] for kind in kinds]

    for combo in itertools.product(*wildcards):
        yield "".join(
            combo[seg] if isinstance(seg, int) else seg for seg in segments
        )


_REDOS_PATTERN = re.compile(
    r"""
    \(               # opening group
    (?:[^()]*         # group contents (non-nested)
       |\[.*?\]       # or character classes
    )*
    [+*]\??          # inner quantifier (+ or * with optional ?)
    (?:[^()]*         # more group contents
       |\[.*?\]       # or character classes
    )*
    \)               # closing group
    [+*]\??          # outer quantifier (+ or * with optional ?)
    """,
    re.VERBOSE,
)


# Timing probe: catastrophic backtracking grows exponentially with input
# length, so it shows up as a blowup across a short ladder of adversarial
# inputs long before any single match becomes slow. Probing bails the moment
# the cumulative budget is spent, which bounds the cost of screening a
# genuinely pathological pattern.
_PROBE_LENGTHS = tuple(range(8, 41, 2))
_PROBE_BUDGET = 0.05  # seconds of cumulative match time before we call it unsafe
_PROBE_MAX_SEEDS = 4


def _check_regex_safety(pattern: str) -> str | None:
    """Check a regex pattern for common ReDoS indicators.

    This is a structural check only; see _probe_regex_blowup for the empirical
    one. Escaped characters are stripped first, since a literal '\\(' is not
    grouping syntax and cannot nest a quantifier.

    Args:
        pattern: The raw regex source.

    Returns:
        An error message if the pattern looks dangerous, or None if it appears
        safe by this check.
    """
    unescaped = re.sub(r"\\.", "", pattern, flags=re.DOTALL)
    if _REDOS_PATTERN.search(unescaped):
        return (
            "pattern contains nested quantifiers which can cause catastrophic "
            "backtracking (ReDoS)"
        )
    return None


def _probe_seeds(pattern: str) -> list[str]:
    """Pick adversarial repeat units for probing, drawn from the pattern itself.

    A pattern only backtracks catastrophically on input built from characters it
    can actually consume, so the literals it mentions make far better probe
    material than a fixed alphabet.

    Args:
        pattern: The raw regex source.

    Returns:
        Repeat units to build probe strings from.
    """
    chars: list[str] = []
    for char in re.findall(r"[A-Za-z0-9]", pattern):
        if char not in chars:
            chars.append(char)
        if len(chars) >= _PROBE_MAX_SEEDS:
            break
    if not chars:
        chars = ["a"]
    seeds = list(chars)
    if len(chars) >= 2:
        seeds.append(chars[0] + chars[1])
    return seeds


def _probe_regex_blowup(regex: "re.Pattern[str]", pattern: str) -> str | None:
    """Time a compiled regex against growing adversarial inputs.

    Args:
        regex: The compiled pattern.
        pattern: Its source, used to choose probe characters.

    Returns:
        An error message if matching blows up, or None if it stays fast.
    """
    seeds = _probe_seeds(pattern)
    spent = 0.0
    for length in _PROBE_LENGTHS:
        for seed in seeds:
            probe = (seed * (length // len(seed) + 1))[:length] + "\x00"
            start = time.perf_counter()
            regex.search(probe)
            spent += time.perf_counter() - start
            if spent > _PROBE_BUDGET:
                return (
                    f"matching took {spent * 1000:.0f} ms on a {length}-character "
                    f"input, which indicates catastrophic backtracking (ReDoS)"
                )
    return None


def _apply_filters(
    words: Iterable[str],
    min_len: int | None,
    max_len: int | None,
    regex: Optional["re.Pattern[str]"],
) -> Generator[str, None, None]:
    """Apply length and regex filters to a word stream.

    Args:
        words: Input word iterable.
        min_len: Minimum word length (inclusive), or None.
        max_len: Maximum word length (inclusive), or None.
        regex: Compiled regex pattern to match, or None.

    Yields:
        Words that pass all filters.
    """
    for word in words:
        if min_len is not None and len(word) < min_len:
            continue
        if max_len is not None and len(word) > max_len:
            continue
        if regex is not None and not regex.search(word):
            continue
        yield word


def _deduplicated(words: Iterable[str]) -> Generator[str, None, None]:
    """Deduplicate words while preserving order.

    Args:
        words: Input word iterable.

    Yields:
        Unique words in order of first appearance.
    """
    seen: set[str] = set()
    for word in words:
        if word not in seen:
            seen.add(word)
            yield word


def _text_writer(
    file_obj: "gzip.GzipFile | io.TextIOBase | io.StringIO",
) -> Callable[[str], None]:
    """Adapt a text-mode or binary-gzip destination to one str-writing call.

    Resolving the text/binary question once, here, keeps it out of the write
    loop and lets the type checker see that each branch writes the right type.

    Args:
        file_obj: The destination file object.

    Returns:
        A function that writes a string to file_obj.
    """
    if isinstance(file_obj, gzip.GzipFile):

        def write_binary(data: str) -> None:
            file_obj.write(data.encode())

        return write_binary

    def write_text(data: str) -> None:
        file_obj.write(data)

    return write_text


_STOP_CHECK_INTERVAL = 10_000  # words between should_stop() polls


@contextlib.contextmanager
def _interrupt_guard() -> Iterator[Callable[[], bool]]:
    """Install a flag-setting SIGINT handler for the duration of the block.

    Yields a predicate reporting whether an interrupt has arrived. Replacing the
    default handler disarms KeyboardInterrupt, so *every* unbounded loop in the
    run has to poll this predicate. Miss one and Ctrl-C is not merely late, it
    is ignored outright.

    Signal handlers can only be installed on the main thread. Off it, the block
    runs under default KeyboardInterrupt behaviour and the predicate stays
    False, so library callers get a working run rather than a ValueError.
    """
    interrupted = False

    def handle(sig: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    try:
        old_handler = signal.signal(signal.SIGINT, handle)
    except ValueError:
        yield lambda: False
        return
    try:
        yield lambda: interrupted
    finally:
        signal.signal(signal.SIGINT, old_handler)


def _interruptible(
    words: Iterable[str], should_stop: Callable[[], bool]
) -> Generator[str, None, None]:
    """Raise KeyboardInterrupt at the source once an interrupt has arrived.

    This belongs on the generator rather than on the writer. Later stages can
    discard every candidate they see -- a filter matching nothing is the
    documented worst case -- and a poll that only runs per *written* word would
    then never run at all, leaving the process deaf to Ctrl-C for the entire
    walk of the pattern space.

    Args:
        words: The raw candidate stream.
        should_stop: Predicate polled every _STOP_CHECK_INTERVAL candidates.

    Yields:
        Each candidate, unchanged.

    Raises:
        KeyboardInterrupt: When should_stop() returns True.
    """
    for seen, word in enumerate(words, 1):
        if seen % _STOP_CHECK_INTERVAL == 0 and should_stop():
            raise KeyboardInterrupt
        yield word


def _chunked_write(
    words: Iterable[str],
    file_obj: "gzip.GzipFile | io.TextIOBase | io.StringIO",
    chunk_size: int,
    progress: _ProgressBar | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Write words to a file in chunks.

    Args:
        words: Word iterable.
        file_obj: File object to write to (text-mode or binary gzip).
        chunk_size: Number of words per chunk.
        progress: Optional tqdm progress bar to update.
        should_stop: Optional predicate polled every _STOP_CHECK_INTERVAL words
            (or every chunk, whichever is smaller). When it returns True the
            write is abandoned.

    Returns:
        Total number of words written.

    Raises:
        KeyboardInterrupt: If should_stop() returns True mid-write.
    """
    write = _text_writer(file_obj)
    total = 0
    buf: list[str] = []

    def flush() -> None:
        nonlocal total
        write("\n".join(buf) + "\n")
        if progress is not None:
            progress.update(len(buf))
        total += len(buf)
        buf.clear()

    check_every = min(chunk_size, _STOP_CHECK_INTERVAL)
    since_check = 0
    for word in words:
        buf.append(word)
        if len(buf) >= chunk_size:
            flush()
        since_check += 1
        if since_check >= check_every:
            since_check = 0
            if should_stop is not None and should_stop():
                raise KeyboardInterrupt
    if buf:
        flush()
    return total


def _needs_dedup(patterns: list[str]) -> bool:
    """Report whether a pattern set can emit the same word twice.

    Deduplication costs memory proportional to the output size, so it is only
    worth paying when duplicates are actually possible:

    * Several patterns can always overlap each other.
    * A single pattern containing '?' can collapse to the same word two ways
      (e.g. '??' over 'x' yields 'x' twice), so it needs deduplication.
    * A single pattern of literals and '*' cannot: itertools.product over a
      deduplicated charset gives distinct combinations at fixed positions.

    Args:
        patterns: The patterns supplied on the command line.

    Returns:
        True if the output stream must be deduplicated.
    """
    if len(patterns) > 1:
        return True
    return "?" in _parse_pattern(patterns[0])[1]


def _human_bytes(size: float) -> str:
    """Format a byte count using the largest unit that keeps it readable.

    Args:
        size: Number of bytes.

    Returns:
        A string such as "95.4 MiB".
    """
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _current_umask() -> int:
    """Read the process umask without leaving it changed.

    Returns:
        The umask currently in effect.
    """
    value = os.umask(0)
    os.umask(value)
    return value


def _remove_if_exists(path: str) -> None:
    """Delete a file, ignoring the case where it was never created.

    Args:
        path: Filesystem path to remove.
    """
    with contextlib.suppress(OSError):
        os.remove(path)


_FORCE_THRESHOLD = 10_000_000  # combinations above which --force is required
_BLOOM_MAX_BYTES = 1 << 31  # 2 GiB ceiling on the approximate-dedup filter


class _BloomFilter:
    """Fixed-memory approximate set membership.

    Exact deduplication costs memory proportional to the output. A Bloom filter
    trades a small, quantified false-positive rate for memory that does not grow
    with the number of words. A false positive here means a word is wrongly
    judged already-seen and dropped, so this is never the default.
    """

    def __init__(self, capacity: int, error_rate: float, max_bytes: int) -> None:
        """Size a filter for the expected number of items.

        Args:
            capacity: Expected number of distinct items.
            error_rate: Target false-positive rate at that capacity.
            max_bytes: Hard ceiling on memory; the achieved rate degrades if the
                ideal size would exceed it.
        """
        self.capacity = max(1, capacity)
        ideal_bits = math.ceil(
            -self.capacity * math.log(error_rate) / (math.log(2) ** 2)
        )
        self.bits = max(8, min(ideal_bits, max_bytes * 8))
        self.hash_count = max(1, round(self.bits / self.capacity * math.log(2)))
        self._array = bytearray((self.bits + 7) // 8)

    @property
    def size_bytes(self) -> int:
        """Memory held by the bit array."""
        return len(self._array)

    @property
    def expected_error_rate(self) -> float:
        """False-positive rate this filter actually achieves at capacity."""
        exponent = -self.hash_count * self.capacity / self.bits
        return float((1.0 - math.exp(exponent)) ** self.hash_count)

    def _positions(self, item: str) -> list[int]:
        """Derive this item's bit positions.

        Args:
            item: The word to hash.

        Returns:
            hash_count bit indices.
        """
        # Kirsch-Mitzenmacher: k probes derived from two hashes. hash() is
        # SipHash with a per-process seed, which is plenty here -- the filter is
        # never persisted or shared, and a measured error rate confirms it
        # tracks theory.
        first = hash(item)
        second = hash(item + "\x00bitbrew") | 1
        return [(first + probe * second) % self.bits for probe in range(self.hash_count)]

    def probably_contains(self, item: str) -> bool:
        """Query membership without recording the item.

        Args:
            item: The word to look up.

        Returns:
            True if the item was probably added, False if it definitely was not.
        """
        return all(
            self._array[position >> 3] & (1 << (position & 7))
            for position in self._positions(item)
        )

    def add_if_absent(self, item: str) -> bool:
        """Record an item, reporting whether it looked new.

        Args:
            item: The word to record.

        Returns:
            True if the item was probably absent, False if probably present.
            False may be wrong at the filter's error rate; True never is.
        """
        array = self._array
        seen = True
        for position in self._positions(item):
            index, mask = position >> 3, 1 << (position & 7)
            if not array[index] & mask:
                seen = False
                array[index] |= mask
        return not seen


def _deduplicated_approx(
    words: Iterable[str], bloom: _BloomFilter
) -> Generator[str, None, None]:
    """Deduplicate through a Bloom filter, in memory bounded by the filter.

    Args:
        words: Input word iterable.
        bloom: The sized filter to record words in.

    Yields:
        Words that the filter judged unseen.
    """
    for word in words:
        if bloom.add_if_absent(word):
            yield word


@dataclasses.dataclass(frozen=True)
class _RunConfig:
    """Everything a run needs, already validated."""

    patterns: list[str]
    charset: str
    regex: "re.Pattern[str] | None"
    min_len: int | None
    max_len: int | None
    limit: int | None
    chunk_size: int
    output_path: str | None
    use_compress: bool
    dedup: str  # "none", "exact" or "approx"
    dedup_error: float
    total_estimate: int
    count_only: bool

    @property
    def discards_candidates(self) -> bool:
        """Whether a stage between generation and output can drop candidates.

        When one can, --limit bounds only the words emitted, not the words
        examined: a filter that matches nothing still walks the whole space.

        Returns:
            True if filtering or deduplication sits in the pipeline.
        """
        return (
            self.min_len is not None
            or self.max_len is not None
            or self.regex is not None
            or self.dedup != "none"
        )

    @property
    def effective_scale(self) -> int:
        """Upper bound on the work the --force guard is being asked to approve.

        --limit caps the output, but it only caps the *work* when nothing can
        discard a candidate on the way out. With a filter or deduplication in
        play the run can still enumerate the entire space looking for matches,
        so the guard has to keep asking about the full estimate.

        Returns:
            The bounded scale of the run.
        """
        if self.limit is None or self.discards_candidates:
            return self.total_estimate
        return min(self.total_estimate, self.limit)

    @property
    def dedup_capacity(self) -> int:
        """How many distinct words approximate deduplication must hold.

        Deduplication runs before filtering and before --limit, so it only gets
        to be limit-sized when nothing downstream can discard a candidate --
        otherwise it can still see every word in the space.

        Returns:
            The capacity to size a Bloom filter for.
        """
        if self.limit is None:
            return self.total_estimate
        if self.min_len is not None or self.max_len is not None or self.regex is not None:
            return self.total_estimate
        return min(self.total_estimate, self.limit)

    @property
    def exact_output_count(self) -> int | None:
        """The output size, when it is knowable without generating anything.

        Returns:
            The exact number of words the run will emit, or None when filters,
            deduplication, or a saturated estimate make it unknowable.
        """
        if self.dedup != "none":
            return None
        if self.min_len is not None or self.max_len is not None:
            return None
        if self.regex is not None or self.total_estimate >= _MAX_ESTIMATE:
            return None
        if self.limit is not None:
            return min(self.total_estimate, self.limit)
        return self.total_estimate


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description="Generate wordlists from patterns using wildcard substitution.",
    )
    parser.add_argument("--version", action="version", version=f"bitbrew {__version__}")
    parser.add_argument(
        "-p", "--pattern", action="append", required=True,
        help="Pattern to expand (repeatable). Use * for one char, ? for zero-or-one "
             "char, and a backslash to escape either.",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Output file path. If omitted, write to stdout.",
    )
    parser.add_argument(
        "--charset", default=None,
        help="Charset preset (lower, upper, digits, symbols, all) or raw chars. "
             "Combine presets with commas: 'lower,digits'. Default: lower.",
    )
    parser.add_argument(
        "--charset-file", default=None,
        help="Read the charset verbatim from a file, allowing commas and spaces.",
    )
    parser.add_argument("--min-len", type=int, default=None, help="Minimum word length.")
    parser.add_argument("--max-len", type=int, default=None, help="Maximum word length.")
    parser.add_argument(
        "--filter", dest="regex_filter", default=None,
        help="Python regex; only matching words are kept.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after N words. Useful for sampling a huge pattern space.",
    )
    parser.add_argument("--count", action="store_true", help="Print count only, no words.")
    parser.add_argument("--compress", action="store_true", help="Write output as .gz.")
    parser.add_argument(
        "--chunk-size", type=int, default=10000,
        help="Stream output N words at a time (default: 10000).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Proceed without confirmation even if >10M combinations.",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Overwrite output file if it already exists.",
    )
    parser.add_argument(
        "--allow-unsafe-regex", action="store_true",
        help="Skip the --filter safety screening. The screening is a heuristic "
             "and does reject some safe patterns.",
    )
    parser.add_argument(
        "--no-dedup", action="store_true",
        help="Stream without deduplicating. Deduplication holds every emitted "
             "word in memory; disable it for very large runs.",
    )
    parser.add_argument(
        "--dedup-approx", action="store_true",
        help="Deduplicate in bounded memory using a Bloom filter. May drop a "
             "small fraction of valid words; see --dedup-error.",
    )
    parser.add_argument(
        "--dedup-error", type=float, default=1e-6,
        help="Target false-positive rate for --dedup-approx (default: 1e-6).",
    )
    return parser


def _resolve_charset_option(args: argparse.Namespace) -> str:
    """Resolve the charset from --charset or --charset-file.

    Args:
        args: Parsed arguments.

    Returns:
        The resolved character string.

    Raises:
        _CliError: If both options are given, or the file cannot be read.
    """
    if args.charset_file is not None:
        if args.charset is not None:
            raise _CliError("--charset and --charset-file are mutually exclusive.")
        try:
            return load_charset_file(args.charset_file)
        except OSError as exc:
            raise _CliError(
                f"could not read --charset-file '{args.charset_file}': {exc}"
            ) from exc
    return resolve_charset(args.charset if args.charset is not None else "lower")


def _resolve_regex_option(args: argparse.Namespace) -> "re.Pattern[str] | None":
    """Compile and screen the --filter regex.

    Args:
        args: Parsed arguments.

    Returns:
        The compiled pattern, or None when no filter was given.

    Raises:
        _CliError: If the regex is invalid or fails safety screening.
    """
    if not args.regex_filter:
        return None
    source = args.regex_filter
    override = " Use --allow-unsafe-regex to run it anyway."

    if not args.allow_unsafe_regex:
        structural = _check_regex_safety(source)
        if structural:
            raise _CliError(f"unsafe regex '{source}': {structural}.{override}")
    try:
        regex = re.compile(source)
    except re.error as exc:
        raise _CliError(f"invalid regex '{source}': {exc}") from exc
    if not args.allow_unsafe_regex:
        timing = _probe_regex_blowup(regex, source)
        if timing:
            raise _CliError(f"unsafe regex '{source}': {timing}.{override}")
    return regex


def _resolve_dedup_option(args: argparse.Namespace) -> str:
    """Decide which deduplication strategy the run uses.

    Args:
        args: Parsed arguments.

    Returns:
        "none", "exact" or "approx".

    Raises:
        _CliError: If conflicting or out-of-range options were given.
    """
    if args.no_dedup and args.dedup_approx:
        raise _CliError("--no-dedup and --dedup-approx are mutually exclusive.")
    if not 0.0 < args.dedup_error < 1.0:
        raise _CliError("--dedup-error must be between 0 and 1, exclusive.")
    if args.no_dedup or not _needs_dedup(args.pattern):
        return "none"
    return "approx" if args.dedup_approx else "exact"


def _resolve_options(args: argparse.Namespace) -> _RunConfig:
    """Validate arguments and resolve them into a run configuration.

    Args:
        args: Parsed arguments.

    Returns:
        The validated configuration.

    Raises:
        _CliError: If any argument is invalid.
    """
    if args.chunk_size <= 0:
        raise _CliError("--chunk-size must be greater than 0.")
    if args.limit is not None and args.limit <= 0:
        raise _CliError("--limit must be greater than 0.")
    for flag, value in (("--min-len", args.min_len), ("--max-len", args.max_len)):
        if value is not None and value < 0:
            raise _CliError(f"{flag} must be zero or greater.")
    if (
        args.min_len is not None
        and args.max_len is not None
        and args.min_len > args.max_len
    ):
        raise _CliError(
            f"--min-len ({args.min_len}) is greater than --max-len ({args.max_len})."
        )

    charset = _resolve_charset_option(args)
    if not charset:
        raise _CliError(
            "resolved charset is empty. Provide a non-empty --charset value."
        )

    total_estimate = 0
    for pattern in args.pattern:
        try:
            _, kinds = _parse_pattern(pattern)
        except ValueError as exc:
            raise _CliError(f"invalid pattern '{pattern}': {exc}") from exc
        if not kinds:
            print(
                f"Warning: pattern '{pattern}' has no wildcards; emitting as literal.",
                file=sys.stderr,
            )
        total_estimate += estimate_count(pattern, len(charset))

    output_path = args.output
    if output_path:
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.isdir(output_dir):
            raise _CliError(f"output directory '{output_dir}' does not exist.")
        if os.path.exists(output_path) and not args.overwrite:
            raise _CliError(
                f"output file '{output_path}' already exists. "
                f"Use --overwrite to replace."
            )

    return _RunConfig(
        patterns=list(args.pattern),
        charset=charset,
        regex=_resolve_regex_option(args),
        min_len=args.min_len,
        max_len=args.max_len,
        limit=args.limit,
        chunk_size=args.chunk_size,
        output_path=output_path,
        use_compress=args.compress or bool(output_path and output_path.endswith(".gz")),
        dedup=_resolve_dedup_option(args),
        dedup_error=args.dedup_error,
        total_estimate=total_estimate,
        count_only=args.count,
    )


def _dedup_stage(words: Iterator[str], cfg: _RunConfig) -> Iterator[str]:
    """Apply the configured deduplication strategy, reporting its cost.

    Args:
        words: Input word stream.
        cfg: Resolved run configuration.

    Returns:
        The deduplicated stream, or the input unchanged when dedup is off.
    """
    if cfg.dedup == "none":
        return words
    if cfg.dedup == "approx":
        bloom = _BloomFilter(cfg.dedup_capacity, cfg.dedup_error, _BLOOM_MAX_BYTES)
        print(
            f"Note: approximate deduplication in {_human_bytes(bloom.size_bytes)}, "
            f"expected false-positive rate {bloom.expected_error_rate:.2g}. "
            f"Some valid words may be dropped.",
            file=sys.stderr,
        )
        return _deduplicated_approx(words, bloom)
    if cfg.total_estimate > _DEDUP_WARN_THRESHOLD:
        print(
            f"Warning: deduplicating ~{cfg.total_estimate:,} words holds them all in "
            f"memory (roughly {_human_bytes(cfg.total_estimate * _BYTES_PER_WORD)}). "
            f"Use --dedup-approx for bounded memory, or --no-dedup to stream.",
            file=sys.stderr,
        )
    return _deduplicated(words)


def _build_pipeline(
    cfg: _RunConfig, should_stop: Callable[[], bool] | None = None
) -> Iterator[str]:
    """Compose the full generate-filter-dedup-limit stream.

    Args:
        cfg: Resolved run configuration.
        should_stop: Optional interrupt predicate, polled at the source.

    Returns:
        The finished word stream.
    """

    def expand() -> Generator[str, None, None]:
        for pattern in cfg.patterns:
            yield from _expand_pattern(pattern, cfg.charset)

    source: Iterable[str] = expand()
    if should_stop is not None:
        source = _interruptible(source, should_stop)

    # Filter before dedup. Both stages are per-word and order-preserving, so
    # the output is identical either way, but this keeps rejected candidates
    # out of the dedup set entirely -- its memory then tracks the output rather
    # than the whole pattern space, which is the tool's real scaling limit.
    words = _dedup_stage(
        _apply_filters(source, cfg.min_len, cfg.max_len, cfg.regex), cfg
    )
    if cfg.limit is not None:
        return itertools.islice(words, cfg.limit)
    return words


def _make_progress(cfg: _RunConfig) -> _ProgressBar | None:
    """Build a tqdm bar when tqdm is installed.

    Args:
        cfg: Resolved run configuration.

    Returns:
        A progress bar, or None when tqdm is unavailable.
    """
    try:
        import tqdm as tqdm_mod
    except ImportError:
        return None
    # Without an exact count a bar could never reach 100%, so show a counter.
    bar: _ProgressBar = tqdm_mod.tqdm(
        total=cfg.exact_output_count, unit="words", desc="Generating"
    )
    return bar


def _write_to_file(
    words: Iterable[str], cfg: _RunConfig, should_stop: Callable[[], bool]
) -> int:
    """Generate into cfg.output_path atomically and interruptibly.

    Args:
        words: The finished word stream.
        cfg: Resolved run configuration.
        should_stop: Interrupt predicate owned by the caller's _interrupt_guard.

    Returns:
        Process exit code.
    """
    if cfg.output_path is None:
        raise ValueError("_write_to_file requires an output path")
    output_path = cfg.output_path

    # Build into a sidecar file and rename on success, so an interrupted or
    # failed run never leaves a truncated wordlist at output_path. The sidecar
    # is created exclusively under a unique name: a fixed one would truncate an
    # unrelated leftover file, and two concurrent runs would corrupt each other.
    temp_path: str | None = None
    progress: _ProgressBar | None = None
    try:
        progress = _make_progress(cfg)
        handle, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(output_path) or ".",
            prefix=os.path.basename(output_path) + ".",
            suffix=".part",
        )
        if cfg.use_compress:
            with (
                os.fdopen(handle, "wb") as raw,
                gzip.GzipFile(fileobj=raw, mode="wb") as binary,
            ):
                written = _chunked_write(
                    words, binary, cfg.chunk_size, progress, should_stop
                )
        else:
            with os.fdopen(handle, "w", encoding="utf-8") as text:
                written = _chunked_write(
                    words, text, cfg.chunk_size, progress, should_stop
                )

        if progress is not None:
            progress.close()

        # A signal can still land between the last poll and here.
        if should_stop():
            raise KeyboardInterrupt

        # mkstemp creates 0600; give the finished file the mode a plain open()
        # would have produced.
        os.chmod(temp_path, 0o666 & ~_current_umask())
        os.replace(temp_path, output_path)
        print(f"Wrote {written:,} words to {output_path}", file=sys.stderr)
    except KeyboardInterrupt:
        if temp_path is not None:
            _remove_if_exists(temp_path)
        print("\nInterrupted. Partial output file removed.", file=sys.stderr)
        return 130
    except OSError as exc:
        if temp_path is not None:
            _remove_if_exists(temp_path)
        print(f"Error: could not write to '{output_path}': {exc}", file=sys.stderr)
        return 1
    finally:
        if progress is not None:
            progress.close()
    return 0


def _detach_stdout() -> None:
    """Point stdout at the null device after the reader has gone away.

    Python flushes stdout again during interpreter shutdown; on a closed pipe
    that raises a second BrokenPipeError and prints "Exception ignored" noise
    after an otherwise ordinary `| head`.
    """
    with contextlib.suppress(OSError, ValueError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())


def _write_to_stdout(words: Iterable[str], cfg: _RunConfig) -> int:
    """Stream words to stdout, gzipped when asked.

    Exit codes match the -o path: 130 when interrupted, 1 when the write
    fails. A closed downstream pipe is the one success case -- `| head`
    getting what it asked for is not an error.

    Args:
        words: The finished word stream.
        cfg: Resolved run configuration.

    Returns:
        Process exit code.
    """
    if not cfg.use_compress:
        # BrokenPipeError subclasses OSError, so it has to be caught first.
        try:
            for word in words:
                print(word)
        except BrokenPipeError:
            _detach_stdout()
            return 0
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        except OSError as exc:
            print(f"Error: could not write to stdout: {exc}", file=sys.stderr)
            return 1
        return 0

    # Gzip to stdout, but never at a terminal -- binary down a TTY is noise.
    if sys.stdout.isatty():
        print(
            "Error: refusing to write compressed output to a terminal. "
            "Redirect it, pipe it, or use -o.",
            file=sys.stderr,
        )
        return 1
    raw = getattr(sys.stdout, "buffer", None)
    if raw is None:
        print(
            "Error: this stdout does not accept binary output; use -o instead.",
            file=sys.stderr,
        )
        return 1
    try:
        with gzip.GzipFile(fileobj=raw, mode="wb") as binary:
            _chunked_write(words, binary, cfg.chunk_size)
    except BrokenPipeError:
        _detach_stdout()
        return 0
    except KeyboardInterrupt:
        # The stream stops mid-member, so it will not decompress. Reporting
        # success here would let `bitbrew ... > out.gz && use out.gz` run on
        # a truncated archive.
        print(
            "\nInterrupted. Compressed output is incomplete.",
            file=sys.stderr,
        )
        return 130
    except OSError as exc:
        print(f"Error: could not write to stdout: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code.
    """
    args = build_parser().parse_args(argv)
    try:
        cfg = _resolve_options(args)
    except _CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Counting is free when nothing can drop a word, so answer before the
    # scale guard: previewing a huge pattern should not need --force.
    if cfg.count_only:
        exact = cfg.exact_output_count
        if exact is not None:
            print(exact)
            return 0

    if cfg.effective_scale > _FORCE_THRESHOLD and not args.force:
        print(
            f"Warning: estimated {cfg.effective_scale:,} combinations. "
            f"Use --force to proceed.",
            file=sys.stderr,
        )
        return 1

    # The guard spans generation as well as writing: with a restrictive filter
    # the pipeline can run for hours without emitting a word, and that stretch
    # has to stay interruptible too.
    with _interrupt_guard() as should_stop:
        words = _build_pipeline(cfg, should_stop)

        if cfg.count_only:
            try:
                print(sum(1 for _ in words))
            except KeyboardInterrupt:
                print("\nInterrupted.", file=sys.stderr)
                return 130
            return 0
        if cfg.output_path:
            return _write_to_file(words, cfg, should_stop)
        return _write_to_stdout(words, cfg)


if __name__ == "__main__":
    sys.exit(main())
