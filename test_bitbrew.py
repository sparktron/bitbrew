"""Tests for bitbrew.py."""

import gzip
import os
import tempfile
import types

import pytest

from bitbrew import (
    _apply_filters,
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
