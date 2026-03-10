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
import gzip
import itertools
import os
import re
import signal
import sys
from typing import Generator, Iterable, Optional

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


def estimate_count(pattern: str, charset_len: int) -> int:
    """Estimate the total number of words a pattern will generate.

    Args:
        pattern: The pattern string with * and ? wildcards.
        charset_len: Number of characters in the active charset.

    Returns:
        Estimated word count.
    """
    count = 1
    for ch in pattern:
        if ch == "*":
            count *= charset_len
        elif ch == "?":
            count *= (charset_len + 1)  # charset options + empty
    return count


def generate_wordlist(pattern: str, charset: str = "lower") -> Generator[str, None, None]:
    """Generate words from a pattern by substituting wildcards.

    Args:
        pattern: Pattern with * (one char) and ? (zero or one char) wildcards.
        charset: A charset preset name or raw character string.

    Yields:
        Generated words.
    """
    chars = resolve_charset(charset)
    _yield_from = _expand_pattern(pattern, chars)
    yield from _yield_from


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
    pos = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            template_parts.append(None)  # placeholder
            wildcards.append(list(chars))
            pos += 1
            i += 1
        elif ch == "?":
            template_parts.append(None)
            wildcards.append([""] + list(chars))  # empty string = skip
            pos += 1
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


def _apply_filters(
    words: Iterable[str],
    min_len: Optional[int],
    max_len: Optional[int],
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


def _chunked_write(
    words: Iterable[str],
    file_obj: "gzip.GzipFile | object",
    chunk_size: int,
    progress: Optional[object] = None,
) -> int:
    """Write words to a file in chunks.

    Args:
        words: Word iterable.
        file_obj: File object to write to (text or gzip).
        chunk_size: Number of words per chunk.
        progress: Optional tqdm progress bar to update.

    Returns:
        Total number of words written.
    """
    total = 0
    buf: list[str] = []
    for word in words:
        buf.append(word)
        if len(buf) >= chunk_size:
            data = "\n".join(buf) + "\n"
            file_obj.write(data.encode() if isinstance(file_obj, gzip.GzipFile) else data)
            if progress is not None:
                progress.update(len(buf))
            total += len(buf)
            buf.clear()
    if buf:
        data = "\n".join(buf) + "\n"
        file_obj.write(data.encode() if isinstance(file_obj, gzip.GzipFile) else data)
        if progress is not None:
            progress.update(len(buf))
        total += len(buf)
    return total


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
    return parser


def main(argv: Optional[list[str]] = None) -> int:
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
        try:
            regex = re.compile(args.regex_filter)
        except re.error as e:
            print(f"Error: invalid regex '{args.regex_filter}': {e}", file=sys.stderr)
            return 1

    # Validate chunk-size
    if args.chunk_size <= 0:
        print("Error: --chunk-size must be greater than 0.", file=sys.stderr)
        return 1

    charset = resolve_charset(args.charset)

    # Validate charset is not empty
    if not charset:
        print("Error: resolved charset is empty. Provide a non-empty --charset value.", file=sys.stderr)
        return 1

    # Validate min-len / max-len relationship
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
            print(f"Warning: pattern '{pattern}' has no wildcards; emitting as literal.", file=sys.stderr)
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

    words = _deduplicated(word_pipeline())
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

        try:
            progress = None
            try:
                import tqdm as tqdm_mod
                progress = tqdm_mod.tqdm(total=total_estimate, unit="words", desc="Generating")
            except ImportError:
                pass

            if use_compress:
                with gzip.open(output_path, "wt", encoding="utf-8") as f:
                    written = _chunked_write(words, f, args.chunk_size, progress)
            else:
                with open(output_path, "w", encoding="utf-8") as f:
                    written = _chunked_write(words, f, args.chunk_size, progress)

            if progress is not None:
                progress.close()

            if interrupted:
                raise KeyboardInterrupt

            print(f"Wrote {written:,} words to {output_path}", file=sys.stderr)

        except KeyboardInterrupt:
            # Clean up partial file
            if os.path.exists(output_path):
                os.remove(output_path)
            print("\nInterrupted. Partial output file removed.", file=sys.stderr)
            return 130
        finally:
            signal.signal(signal.SIGINT, old_handler)
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
