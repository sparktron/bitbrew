#!/usr/bin/env python3
"""bitbrew — pattern-based wordlist generator CLI tool.

Generates wordlists from patterns using wildcard substitution with support for
multiple charsets, filtering, streaming output, and compression.

Pattern syntax:
    * — matches exactly one character from the active charset
    ? — optional: matches zero or one character (produces two variants)
    Literal characters are preserved as-is

Usage as a library:
    from bitbrew import generate_wordlist
    for word in generate_wordlist("pass*", "digits"):
        print(word)
"""

import argparse
import contextlib
import gzip
import io
import itertools
import os
import re
import signal
import sys
import time
from collections.abc import Callable, Generator, Iterable
from typing import Optional

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
    # Deduplicate while preserving order
    seen: set[str] = set()
    result = []
    for c in chars:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return "".join(result)


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
    """
    count = 1
    for ch in pattern:
        if ch == "*":
            count *= charset_len
        elif ch == "?":
            count *= (charset_len + 1)  # charset options + empty
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


def _expand_pattern(pattern: str, chars: str) -> Generator[str, None, None]:
    """Expand a pattern into all matching words.

    Args:
        pattern: The pattern string.
        chars: The resolved charset characters.

    Yields:
        Generated words.
    """
    # Parse pattern into segments: each segment is either a literal string
    # or a wildcard descriptor
    wildcards = []
    template_parts = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            template_parts.append(None)  # placeholder
            wildcards.append(list(chars))
            i += 1
        elif ch == "?":
            template_parts.append(None)
            wildcards.append([""] + list(chars))  # empty string = skip
            i += 1
        else:
            # Literal character
            template_parts.append(ch)
            i += 1

    if not wildcards:
        # No wildcards — yield the literal pattern
        yield pattern
        return

    # Build template: merge consecutive literals
    segments: list[str | int] = []  # str = literal, int = wildcard index
    wc_idx = 0
    literal_buf = ""
    for part in template_parts:
        if part is None:
            if literal_buf:
                segments.append(literal_buf)
                literal_buf = ""
            segments.append(wc_idx)
            wc_idx += 1
        else:
            literal_buf += part
    if literal_buf:
        segments.append(literal_buf)

    for combo in itertools.product(*wildcards):
        parts = []
        for seg in segments:
            if isinstance(seg, int):
                parts.append(combo[seg])
            else:
                parts.append(seg)
        yield "".join(parts)


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


_STOP_CHECK_INTERVAL = 10_000  # words between should_stop() polls


def _chunked_write(
    words: Iterable[str],
    file_obj: "gzip.GzipFile | io.TextIOBase | io.StringIO",
    chunk_size: int,
    progress: object | None = None,
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
    is_binary = isinstance(file_obj, gzip.GzipFile)
    total = 0
    buf: list[str] = []

    def flush() -> None:
        nonlocal total
        data = "\n".join(buf) + "\n"
        file_obj.write(data.encode() if is_binary else data)
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
    return "?" in patterns[0]


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


def _remove_if_exists(path: str) -> None:
    """Delete a file, ignoring the case where it was never created.

    Args:
        path: Filesystem path to remove.
    """
    with contextlib.suppress(OSError):
        os.remove(path)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI.

    Returns:
        Configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description="Generate wordlists from patterns using wildcard substitution.",
    )
    parser.add_argument(
        "-p", "--pattern", action="append", required=True,
        help="Pattern to expand (repeatable). Use * for one char, ? for zero-or-one char.",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Output file path. If omitted, write to stdout.",
    )
    parser.add_argument(
        "--charset", default="lower",
        help="Charset preset (lower, upper, digits, symbols, all) or raw chars. "
             "Combine presets with commas: 'lower,digits'. Default: lower.",
    )
    parser.add_argument("--min-len", type=int, default=None, help="Minimum word length.")
    parser.add_argument("--max-len", type=int, default=None, help="Maximum word length.")
    parser.add_argument(
        "--filter", dest="regex_filter", default=None,
        help="Python regex; only matching words are kept.",
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
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Validate regex filter
    regex = None
    if args.regex_filter:
        if not args.allow_unsafe_regex:
            safety_err = _check_regex_safety(args.regex_filter)
            if safety_err:
                print(
                    f"Error: unsafe regex '{args.regex_filter}': {safety_err}. "
                    f"Use --allow-unsafe-regex to run it anyway.",
                    file=sys.stderr,
                )
                return 1
        try:
            regex = re.compile(args.regex_filter)
        except re.error as e:
            print(f"Error: invalid regex '{args.regex_filter}': {e}", file=sys.stderr)
            return 1
        if not args.allow_unsafe_regex:
            probe_err = _probe_regex_blowup(regex, args.regex_filter)
            if probe_err:
                print(
                    f"Error: unsafe regex '{args.regex_filter}': {probe_err}. "
                    f"Use --allow-unsafe-regex to run it anyway.",
                    file=sys.stderr,
                )
                return 1

    # Validate chunk-size
    if args.chunk_size <= 0:
        print("Error: --chunk-size must be greater than 0.", file=sys.stderr)
        return 1

    charset = resolve_charset(args.charset)

    # Validate charset is not empty
    if not charset:
        print(
            "Error: resolved charset is empty. Provide a non-empty --charset value.",
            file=sys.stderr,
        )
        return 1

    # Validate min-len / max-len
    for flag_name, value in (("--min-len", args.min_len), ("--max-len", args.max_len)):
        if value is not None and value < 0:
            print(f"Error: {flag_name} must be zero or greater.", file=sys.stderr)
            return 1

    if args.min_len is not None and args.max_len is not None and args.min_len > args.max_len:
        print(
            f"Error: --min-len ({args.min_len}) is greater than --max-len ({args.max_len}).",
            file=sys.stderr,
        )
        return 1

    # Estimate total count and warn
    total_estimate = 0
    for pattern in args.pattern:
        has_wildcard = any(c in pattern for c in "*?")
        if not has_wildcard:
            print(
                f"Warning: pattern '{pattern}' has no wildcards; emitting as literal.",
                file=sys.stderr,
            )
        total_estimate += estimate_count(pattern, len(charset))

    if total_estimate > 10_000_000 and not args.force:
        print(
            f"Warning: estimated {total_estimate:,} combinations. "
            f"Use --force to proceed.",
            file=sys.stderr,
        )
        return 1

    # Check output file
    output_path = args.output
    use_compress = args.compress
    if output_path and output_path.endswith(".gz"):
        use_compress = True

    if output_path:
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.isdir(output_dir):
            print(
                f"Error: output directory '{output_dir}' does not exist.",
                file=sys.stderr,
            )
            return 1

    if output_path and os.path.exists(output_path) and not args.overwrite:
        print(
            f"Error: output file '{output_path}' already exists. Use --overwrite to replace.",
            file=sys.stderr,
        )
        return 1

    # Build word generator pipeline
    def word_pipeline() -> Generator[str, None, None]:
        for pattern in args.pattern:
            yield from _expand_pattern(pattern, charset)

    words: Iterable[str] = word_pipeline()
    dedup_active = not args.no_dedup and _needs_dedup(args.pattern)
    if dedup_active:
        if total_estimate > _DEDUP_WARN_THRESHOLD:
            print(
                f"Warning: deduplicating ~{total_estimate:,} words holds them all in "
                f"memory (roughly {_human_bytes(total_estimate * _BYTES_PER_WORD)}). "
                f"Use --no-dedup to stream instead.",
                file=sys.stderr,
            )
        words = _deduplicated(words)
    words = _apply_filters(words, args.min_len, args.max_len, regex)

    # Count-only mode
    if args.count:
        count = sum(1 for _ in words)
        print(count)
        return 0

    # Output
    if output_path:
        # Set up interrupt handler to clean up partial file
        interrupted = False

        def _handle_interrupt(sig: int, frame: object) -> None:
            nonlocal interrupted
            interrupted = True

        old_handler = signal.signal(signal.SIGINT, _handle_interrupt)

        # Build into a sidecar file and rename on success, so an interrupted
        # or failed run never leaves a truncated wordlist at output_path.
        temp_path = output_path + ".part"
        progress = None
        try:
            # total_estimate is an upper bound: filters and dedup drop words, and
            # a saturated estimate is not a real number at all. Fall back to a
            # plain counter rather than a bar that can never reach 100%.
            exact_total = (
                total_estimate < _MAX_ESTIMATE
                and args.min_len is None
                and args.max_len is None
                and regex is None
                and not dedup_active
            )
            try:
                import tqdm as tqdm_mod
                progress = tqdm_mod.tqdm(
                    total=total_estimate if exact_total else None,
                    unit="words", desc="Generating",
                )
            except ImportError:
                pass

            if use_compress:
                with gzip.open(temp_path, "wb") as f:
                    written = _chunked_write(
                        words, f, args.chunk_size, progress, lambda: interrupted
                    )
            else:
                with open(temp_path, "w", encoding="utf-8") as f:
                    written = _chunked_write(
                        words, f, args.chunk_size, progress, lambda: interrupted
                    )

            if progress is not None:
                progress.close()

            # A signal can still land between the last poll and here.
            if interrupted:
                raise KeyboardInterrupt

            os.replace(temp_path, output_path)
            print(f"Wrote {written:,} words to {output_path}", file=sys.stderr)

        except KeyboardInterrupt:
            _remove_if_exists(temp_path)
            print("\nInterrupted. Partial output file removed.", file=sys.stderr)
            return 130
        except OSError as e:
            _remove_if_exists(temp_path)
            print(f"Error: could not write to '{output_path}': {e}", file=sys.stderr)
            return 1
        finally:
            if progress is not None:
                progress.close()
            signal.signal(signal.SIGINT, old_handler)
    elif use_compress:
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
            with gzip.GzipFile(fileobj=raw, mode="wb") as f:
                _chunked_write(words, f, args.chunk_size)
        except (BrokenPipeError, KeyboardInterrupt):
            return 0
    else:
        # Write to stdout
        try:
            for word in words:
                print(word)
        except (BrokenPipeError, KeyboardInterrupt):
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
