"""Tests for bitbrew.py."""

import gzip
import io
import os
import signal
import tempfile
import threading
import time
import types
from unittest import mock

import pytest

from bitbrew import (
    _MAX_ESTIMATE,
    _apply_filters,
    _check_regex_safety,
    _chunked_write,
    _deduplicated,
    _expand_pattern,
    estimate_count,
    generate_wordlist,
    main,
    resolve_charset,
)


class TestResolveCharset:
    def test_preset_lower(self) -> None:
        assert resolve_charset("lower") == "abcdefghijklmnopqrstuvwxyz"

    def test_preset_digits(self) -> None:
        assert resolve_charset("digits") == "0123456789"

    def test_preset_combined(self) -> None:
        result = resolve_charset("lower,digits")
        assert result.startswith("abcdefghijklmnopqrstuvwxyz")
        assert result.endswith("0123456789")

    def test_raw_string(self) -> None:
        assert resolve_charset("abc123") == "abc123"

    def test_deduplication(self) -> None:
        assert resolve_charset("aabbc") == "abc"


class TestEstimateCount:
    def test_one_wildcard(self) -> None:
        assert estimate_count("a*", 26) == 26

    def test_two_wildcards(self) -> None:
        assert estimate_count("**", 26) == 26 * 26

    def test_three_wildcards(self) -> None:
        assert estimate_count("***", 10) == 1000

    def test_optional_wildcard(self) -> None:
        assert estimate_count("a?", 26) == 27  # 26 + 1 (empty)

    def test_no_wildcards(self) -> None:
        assert estimate_count("hello", 26) == 1


class TestExpandPattern:
    def test_single_star(self) -> None:
        results = list(_expand_pattern("a*", "xy"))
        assert results == ["ax", "ay"]

    def test_two_stars(self) -> None:
        results = list(_expand_pattern("**", "ab"))
        assert sorted(results) == ["aa", "ab", "ba", "bb"]

    def test_optional_wildcard(self) -> None:
        results = list(_expand_pattern("a?b", "x"))
        assert sorted(results) == ["ab", "axb"]

    def test_no_wildcards(self) -> None:
        results = list(_expand_pattern("hello", "abc"))
        assert results == ["hello"]

    def test_returns_generator(self) -> None:
        result = _expand_pattern("*", "ab")
        assert isinstance(result, types.GeneratorType)


class TestGenerateWordlist:
    def test_basic(self) -> None:
        words = list(generate_wordlist("a*", "digits"))
        assert len(words) == 10
        assert "a0" in words
        assert "a9" in words

    def test_custom_charset(self) -> None:
        words = list(generate_wordlist("*", "abc"))
        assert words == ["a", "b", "c"]

    def test_three_wildcards_count(self) -> None:
        words = list(generate_wordlist("***", "ab"))
        assert len(words) == 8  # 2^3


class TestFilters:
    def test_min_len(self) -> None:
        words = ["a", "ab", "abc", "abcd"]
        result = list(_apply_filters(words, min_len=3, max_len=None, regex=None))
        assert result == ["abc", "abcd"]

    def test_max_len(self) -> None:
        words = ["a", "ab", "abc", "abcd"]
        result = list(_apply_filters(words, min_len=None, max_len=2, regex=None))
        assert result == ["a", "ab"]

    def test_min_and_max_len(self) -> None:
        words = ["a", "ab", "abc", "abcd"]
        result = list(_apply_filters(words, min_len=2, max_len=3, regex=None))
        assert result == ["ab", "abc"]

    def test_regex_filter(self) -> None:
        import re
        words = ["cat", "bat", "car", "bar"]
        pattern = re.compile(r"^c")
        result = list(_apply_filters(words, min_len=None, max_len=None, regex=pattern))
        assert result == ["cat", "car"]


class TestCheckRegexSafety:
    def test_safe_patterns_pass(self) -> None:
        safe = [r"^abc$", r"\d+", r"[a-z]+", r"(foo|bar)", r"a{2,5}"]
        for pat in safe:
            assert _check_regex_safety(pat) is None, f"should be safe: {pat}"

    def test_nested_quantifiers_rejected(self) -> None:
        dangerous = [r"(a+)+", r"(a+)*", r"(a*)+", r"(a*)*", r"(x+)+?", r"([a-z]+)*"]
        for pat in dangerous:
            result = _check_regex_safety(pat)
            assert result is not None, f"should be rejected: {pat}"
            assert "nested quantifiers" in result

    def test_cli_rejects_redos_pattern(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", "ab", "--filter", "(a+)+$"])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "unsafe regex" in stderr


class TestDeduplication:
    def test_removes_duplicates(self) -> None:
        words = ["a", "b", "a", "c", "b"]
        result = list(_deduplicated(words))
        assert result == ["a", "b", "c"]

    def test_preserves_order(self) -> None:
        words = ["c", "a", "b", "a"]
        result = list(_deduplicated(words))
        assert result == ["c", "a", "b"]


class TestCLI:
    def test_stdout_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", "xy"])
        assert ret == 0
        output = capsys.readouterr().out.strip().split("\n")
        assert sorted(output) == ["ax", "ay"]

    def test_count_mode(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "**", "--charset", "ab", "--count"])
        assert ret == 0
        output = capsys.readouterr().out.strip()
        assert output == "4"

    def test_file_output(self, tmp_path: "os.PathLike[str]") -> None:
        outfile = str(tmp_path / "words.txt")
        ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 0
        content = open(outfile).read().strip().split("\n")
        assert sorted(content) == ["ax", "ay"]

    def test_gz_output(self, tmp_path: "os.PathLike[str]") -> None:
        outfile = str(tmp_path / "words.txt.gz")
        ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 0
        with gzip.open(outfile, "rt") as f:
            content = f.read().strip().split("\n")
        assert sorted(content) == ["ax", "ay"]

    def test_overwrite_guard(self, tmp_path: "os.PathLike[str]") -> None:
        outfile = str(tmp_path / "words.txt")
        open(outfile, "w").close()
        ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 1  # should fail

    def test_overwrite_flag(self, tmp_path: "os.PathLike[str]") -> None:
        outfile = str(tmp_path / "words.txt")
        open(outfile, "w").close()
        ret = main(["-p", "a*", "--charset", "xy", "-o", outfile, "--overwrite"])
        assert ret == 0

    def test_force_large(self, capsys: pytest.CaptureFixture[str]) -> None:
        # 26^6 > 10M, should fail without --force
        ret = main(["-p", "******", "--charset", "lower"])
        assert ret == 1

    def test_invalid_regex(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--filter", "[invalid"])
        assert ret == 1

    def test_min_max_len_cli(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a?", "--charset", "xy", "--min-len", "2"])
        assert ret == 0
        output = capsys.readouterr().out.strip().split("\n")
        assert sorted(output) == ["ax", "ay"]
        # "a" alone (from ? = empty) has length 1, should be filtered out

    def test_regex_filter_cli(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "*at", "--charset", "bcr", "--filter", "^c"])
        assert ret == 0
        output = capsys.readouterr().out.strip().split("\n")
        assert output == ["cat"]

    def test_multiple_patterns_dedup(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "-p", "a*", "--charset", "xy"])
        assert ret == 0
        output = capsys.readouterr().out.strip().split("\n")
        assert sorted(output) == ["ax", "ay"]  # deduplicated

    def test_no_wildcard_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "hello"])
        assert ret == 0
        stderr = capsys.readouterr().err
        assert "no wildcards" in stderr

    def test_chunk_size_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", "xy", "--chunk-size", "0"])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "--chunk-size must be greater than 0" in stderr

    def test_chunk_size_negative(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", "xy", "--chunk-size", "-5"])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "--chunk-size must be greater than 0" in stderr

    def test_min_len_greater_than_max_len(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", "xy", "--min-len", "5", "--max-len", "3"])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "--min-len (5) is greater than --max-len (3)" in stderr

    def test_empty_charset(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", ""])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "charset is empty" in stderr


class TestChunkedWriteEdgeCases:
    """Edge-case tests for _chunked_write at the unit level."""

    def test_chunk_size_one(self) -> None:
        """Chunk size of 1 should flush every word individually."""
        buf = io.StringIO()
        words = ["alpha", "beta", "gamma"]
        total = _chunked_write(words, buf, chunk_size=1)
        assert total == 3
        assert buf.getvalue() == "alpha\nbeta\ngamma\n"

    def test_chunk_size_larger_than_input(self) -> None:
        """When chunk_size exceeds word count, everything goes in one flush."""
        buf = io.StringIO()
        words = ["a", "b"]
        total = _chunked_write(words, buf, chunk_size=1000)
        assert total == 2
        assert buf.getvalue() == "a\nb\n"

    def test_exact_chunk_boundary(self) -> None:
        """Words exactly divisible by chunk_size should leave no remainder."""
        buf = io.StringIO()
        words = ["w1", "w2", "w3", "w4"]
        total = _chunked_write(words, buf, chunk_size=2)
        assert total == 4

    def test_empty_word_stream(self) -> None:
        """An empty word iterable writes nothing."""
        buf = io.StringIO()
        total = _chunked_write([], buf, chunk_size=10)
        assert total == 0
        assert buf.getvalue() == ""

    def test_gzip_write(self, tmp_path: "os.PathLike[str]") -> None:
        """Chunked write into a gzip file object encodes correctly."""
        gz_path = str(tmp_path / "out.gz")
        with gzip.open(gz_path, "wb") as f:
            total = _chunked_write(["hello", "world"], f, chunk_size=1)
        assert total == 2
        with gzip.open(gz_path, "rt") as f:
            assert f.read() == "hello\nworld\n"

    def test_progress_callback(self) -> None:
        """Progress object's update() is called with correct batch sizes."""
        buf = io.StringIO()
        progress = mock.MagicMock()
        words = ["a", "b", "c", "d", "e"]
        _chunked_write(words, buf, chunk_size=2, progress=progress)
        # Two full chunks of 2, one remainder of 1
        calls = [c.args[0] for c in progress.update.call_args_list]
        assert calls == [2, 2, 1]


class TestGzipCorruptionAndPermissionErrors:
    """Tests for gzip corruption detection and write-permission failures."""

    def test_corrupted_gz_output_detectable(self, tmp_path: "os.PathLike[str]") -> None:
        """A truncated/corrupted .gz file should raise on read."""
        gz_path = str(tmp_path / "words.gz")
        ret = main(["-p", "a*", "--charset", "xy", "-o", gz_path])
        assert ret == 0
        # Corrupt the file by truncating it
        with open(gz_path, "r+b") as f:
            f.truncate(5)
        with pytest.raises((gzip.BadGzipFile, EOFError)):
            with gzip.open(gz_path, "rt") as f:
                f.read()

    def test_write_permission_error(self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]") -> None:
        """Writing to an unwritable path should return 1 with an error message."""
        outfile = str(tmp_path / "words.txt")
        with mock.patch("builtins.open", side_effect=PermissionError("Permission denied")):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "could not write" in stderr

    def test_gz_write_error(self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]") -> None:
        """Gzip open failure should return 1 with an error message."""
        outfile = str(tmp_path / "words.gz")
        with mock.patch("gzip.open", side_effect=OSError("disk full")):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "could not write" in stderr


class TestInterruptCleanup:
    """Tests for SIGINT handler and partial file cleanup."""

    def test_interrupt_removes_partial_file(self, tmp_path: "os.PathLike[str]") -> None:
        """Ctrl+C during file write should remove the partial output file."""
        outfile = str(tmp_path / "partial.txt")

        # Patch _chunked_write to simulate an interrupt mid-write
        original_chunked_write = _chunked_write

        def interrupting_write(words, file_obj, chunk_size, progress=None):
            # Write some data then send SIGINT to ourselves
            count = 0
            for word in words:
                file_obj.write(word + "\n")
                count += 1
                if count >= 1:
                    os.kill(os.getpid(), signal.SIGINT)
                    break
            return count

        with mock.patch("bitbrew._chunked_write", side_effect=interrupting_write):
            ret = main(["-p", "a*", "--charset", "abcde", "-o", outfile])

        assert ret == 130
        # The partial file should have been removed
        assert not os.path.exists(outfile)

    def test_interrupt_returns_130(self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]") -> None:
        """Interrupted execution should return exit code 130."""
        outfile = str(tmp_path / "int.txt")

        def raise_interrupt(words, file_obj, chunk_size, progress=None):
            raise KeyboardInterrupt

        with mock.patch("bitbrew._chunked_write", side_effect=raise_interrupt):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])

        assert ret == 130
        stderr = capsys.readouterr().err
        assert "Interrupted" in stderr

    def test_interrupt_restores_old_handler(self, tmp_path: "os.PathLike[str]") -> None:
        """After interrupt handling, the original SIGINT handler should be restored."""
        outfile = str(tmp_path / "restore.txt")
        old_handler = signal.getsignal(signal.SIGINT)

        def raise_interrupt(words, file_obj, chunk_size, progress=None):
            raise KeyboardInterrupt

        with mock.patch("bitbrew._chunked_write", side_effect=raise_interrupt):
            main(["-p", "a*", "--charset", "xy", "-o", outfile])

        current_handler = signal.getsignal(signal.SIGINT)
        assert current_handler is old_handler

    def test_stdout_keyboard_interrupt(self, capsys: pytest.CaptureFixture[str]) -> None:
        """KeyboardInterrupt on stdout path should return 0 gracefully."""
        # Patch print to raise KeyboardInterrupt after first word
        call_count = 0
        original_print = print

        def interrupting_print(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyboardInterrupt
            original_print(*args, **kwargs)

        with mock.patch("builtins.print", side_effect=interrupting_print):
            ret = main(["-p", "a*", "--charset", "xy"])
        assert ret == 0


class TestStressAndPerformance:
    """Stress and performance tests for large pattern spaces."""

    def test_large_pattern_space_generator_is_lazy(self) -> None:
        """generate_wordlist should be lazy; creating it shouldn't consume memory."""
        # 26^4 = 456,976 words — but as a generator it shouldn't allocate them all
        gen = generate_wordlist("****", "lower")
        assert isinstance(gen, types.GeneratorType)
        # Consume just the first 10 to prove laziness
        first_10 = [next(gen) for _ in range(10)]
        assert len(first_10) == 10

    def test_large_expansion_count(self) -> None:
        """Verify estimate is correct for larger patterns."""
        # 10^5 = 100,000
        assert estimate_count("*****", 10) == 100_000

    def test_large_chunked_write_performance(self, tmp_path: "os.PathLike[str]") -> None:
        """Writing 100k words in chunks should complete without issue."""
        outfile = str(tmp_path / "large.txt")

        def word_gen():
            for i in range(100_000):
                yield f"word{i}"

        with open(outfile, "w") as f:
            total = _chunked_write(word_gen(), f, chunk_size=5000)
        assert total == 100_000
        # Verify file has correct line count
        with open(outfile) as f:
            lines = f.readlines()
        assert len(lines) == 100_000

    def test_large_pattern_with_filters(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Filters should work correctly even with large pattern spaces."""
        # Generate ab,ac,...,zz with min_len=2, max_len=2 (all pass for **)
        ret = main(["-p", "**", "--charset", "digits", "--min-len", "2", "--max-len", "2", "--count"])
        assert ret == 0
        output = capsys.readouterr().out.strip()
        assert output == "100"  # 10 * 10

    def test_deduplicate_large_overlapping_patterns(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Deduplication should handle heavily overlapping multi-pattern input."""
        ret = main(["-p", "a*", "-p", "a*", "-p", "a*", "--charset", "xy", "--count"])
        assert ret == 0
        output = capsys.readouterr().out.strip()
        assert output == "2"  # ax, ay — all three patterns deduplicated


class TestAdditionalEdgeCases:
    """Additional edge cases for thorough coverage."""

    def test_pattern_all_wildcards(self) -> None:
        """Pattern that is entirely wildcards."""
        words = list(_expand_pattern("***", "ab"))
        assert len(words) == 8  # 2^3

    def test_pattern_all_optional(self) -> None:
        """Pattern that is entirely optional wildcards."""
        words = list(_expand_pattern("??", "x"))
        # Each ? has ["", "x"]: ("",""), ("","x"), ("x",""), ("x","x")
        assert sorted(words) == ["", "x", "x", "xx"]

    def test_filter_all_excluded(self) -> None:
        """All words filtered out should yield empty."""
        words = ["a", "b", "c"]
        result = list(_apply_filters(words, min_len=10, max_len=None, regex=None))
        assert result == []

    def test_filter_none_excluded(self) -> None:
        """No filters applied should pass everything through."""
        words = ["a", "bb", "ccc"]
        result = list(_apply_filters(words, min_len=None, max_len=None, regex=None))
        assert result == ["a", "bb", "ccc"]

    def test_resolve_charset_single_char(self) -> None:
        """Single-character charset should work."""
        assert resolve_charset("x") == "x"

    def test_resolve_charset_all_duplicates(self) -> None:
        """All-duplicate input should resolve to unique chars."""
        assert resolve_charset("aaaa") == "a"

    def test_expand_empty_charset(self) -> None:
        """Expanding a wildcard pattern with empty charset yields nothing."""
        words = list(_expand_pattern("a*", ""))
        assert words == []

    def test_expand_single_char_charset(self) -> None:
        """Single-char charset produces exactly one word per wildcard."""
        words = list(_expand_pattern("**", "z"))
        assert words == ["zz"]

    def test_cli_compress_flag_non_gz_extension(self, tmp_path: "os.PathLike[str]") -> None:
        """--compress flag should produce valid gzip even without .gz extension."""
        outfile = str(tmp_path / "words.txt")
        ret = main(["-p", "a*", "--charset", "xy", "-o", outfile, "--compress"])
        assert ret == 0
        with gzip.open(outfile, "rt") as f:
            content = f.read().strip().split("\n")
        assert sorted(content) == ["ax", "ay"]

    def test_cli_count_with_filters(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Count mode should respect filters."""
        ret = main(["-p", "a?", "--charset", "xy", "--min-len", "2", "--count"])
        assert ret == 0
        output = capsys.readouterr().out.strip()
        assert output == "2"  # "ax" and "ay" pass, "a" does not

    def test_mixed_wildcards(self) -> None:
        """Pattern with both * and ? wildcards."""
        words = list(_expand_pattern("*?", "ab"))
        # * -> [a,b], ? -> ["",a,b]
        # a+"", a+a, a+b, b+"", b+a, b+b
        assert sorted(words) == ["a", "aa", "ab", "b", "ba", "bb"]

    def test_special_chars_in_pattern_literal(self) -> None:
        """Literal special characters (not * or ?) should pass through."""
        words = list(_expand_pattern("hello!", "ab"))
        assert words == ["hello!"]

    def test_overwrite_guard_with_gz(self, tmp_path: "os.PathLike[str]") -> None:
        """Overwrite guard should also apply to .gz files."""
        outfile = str(tmp_path / "words.gz")
        open(outfile, "w").close()
        ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 1


class TestAuditFixes:
    """Tests for issues discovered during the codebase audit."""

    def test_output_dir_does_not_exist(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Writing to a non-existent directory should return a clear error."""
        ret = main(["-p", "a*", "--charset", "xy", "-o", "/no/such/dir/words.txt"])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "directory" in stderr and "does not exist" in stderr

    def test_oserror_during_write(self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]") -> None:
        """OSError during file write should return 1 with a message, not crash."""
        outfile = str(tmp_path / "words.txt")
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "could not write" in stderr

    def test_estimate_count_capped(self) -> None:
        """estimate_count should cap at _MAX_ESTIMATE for huge patterns."""
        # 70^50 is astronomically large
        result = estimate_count("*" * 50, 70)
        assert result == _MAX_ESTIMATE

    def test_redos_with_char_class(self) -> None:
        """ReDoS checker should catch nested quantifiers with character classes."""
        result = _check_regex_safety(r"([a-z]+)+")
        assert result is not None
        assert "nested quantifiers" in result

    def test_gzip_binary_mode_write(self, tmp_path: "os.PathLike[str]") -> None:
        """gzip output should produce valid gzip data via binary-mode write."""
        outfile = str(tmp_path / "words.gz")
        ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 0
        with gzip.open(outfile, "rt") as f:
            content = f.read().strip().split("\n")
        assert sorted(content) == ["ax", "ay"]
