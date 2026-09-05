"""Tests for bitbrew.py."""

import errno
import gzip
import importlib.metadata
import io
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
from unittest import mock

import pytest

import bitbrew
from bitbrew import (
    _BLOOM_MAX_BYTES,
    _MAX_ESTIMATE,
    _apply_filters,
    _BloomFilter,
    _check_regex_safety,
    _chunked_write,
    _deduplicated,
    _expand_pattern,
    _needs_dedup,
    _parse_pattern,
    _plan_bloom,
    _probe_regex_blowup,
    _resolve_options,
    build_parser,
    estimate_count,
    generate_wordlist,
    load_charset_file,
    main,
    resolve_charset,
)

BITBREW_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bitbrew.py")


def _sidecars(directory: "os.PathLike[str] | str") -> list[str]:
    """Leftover .part sidecar files in a directory."""
    return [name for name in os.listdir(str(directory)) if name.endswith(".part")]


class _BinaryStdout:
    """A non-tty stdout stand-in exposing .buffer, for --compress tests."""

    def __init__(self, binary: "io.BufferedWriter") -> None:
        self.buffer = binary

    def isatty(self) -> bool:
        return False


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

    @pytest.mark.parametrize("pattern", [r"(a\+)+", r"(\*)+", r"([+])+", r"([]+])+"])
    def test_literal_quantifier_characters_are_allowed(self, pattern: str) -> None:
        """Escaped and character-class metacharacters are not repetitions."""
        assert _check_regex_safety(pattern) is None

    def test_structural_check_is_linear_on_malformed_class(self) -> None:
        """Malformed input must reach re.compile without detector backtracking."""
        pattern = "(" + "[" * 24 + "a)+"
        start = time.perf_counter()
        assert _check_regex_safety(pattern) is None
        assert time.perf_counter() - start < 0.05

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
        with open(outfile) as f:
            content = f.read().strip().split("\n")
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
        with pytest.raises((gzip.BadGzipFile, EOFError)), gzip.open(gz_path, "rt") as f:
            f.read()

    def test_write_permission_error(
        self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]"
    ) -> None:
        """Writing to an unwritable path should return 1 with an error message."""
        outfile = str(tmp_path / "words.txt")
        # Creating the sidecar is the first thing that touches the filesystem.
        with mock.patch(
            "bitbrew.tempfile.mkstemp", side_effect=PermissionError("Permission denied")
        ):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 1
        assert "could not write" in capsys.readouterr().err
        assert not os.path.exists(outfile)

    def test_gz_write_error(
        self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]"
    ) -> None:
        """Gzip stream failure should return 1 with an error message."""
        outfile = str(tmp_path / "words.gz")
        with mock.patch("bitbrew.gzip.GzipFile", side_effect=OSError("disk full")):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 1
        assert "could not write" in capsys.readouterr().err
        assert not os.path.exists(outfile)
        assert not _sidecars(tmp_path)


class TestInterruptCleanup:
    """Tests for SIGINT handling, partial file cleanup, and atomic rename."""

    def test_should_stop_aborts_write_loop(self) -> None:
        """_chunked_write must poll should_stop and abandon an endless stream."""
        buf = io.StringIO()
        stop = {"flag": False}
        flushes = []

        class TripOnFirstFlush:
            """Stands in for tqdm; trips the stop flag when a chunk lands."""

            def update(self, n: int) -> None:
                flushes.append(n)
                stop["flag"] = True

        def endless():
            i = 0
            while True:
                yield f"w{i}"
                i += 1

        with pytest.raises(KeyboardInterrupt):
            _chunked_write(
                endless(), buf, chunk_size=100,
                progress=TripOnFirstFlush(), should_stop=lambda: stop["flag"],
            )
        # Stopped at the first poll after the first chunk, not run forever.
        assert flushes == [100]

    def test_should_stop_none_writes_everything(self) -> None:
        """Omitting should_stop keeps the original write-to-completion behaviour."""
        buf = io.StringIO()
        total = _chunked_write(["a", "b", "c"], buf, chunk_size=2, should_stop=None)
        assert total == 3
        assert buf.getvalue() == "a\nb\nc\n"

    def test_sigint_handler_interrupts_current_operation(self) -> None:
        """SIGINT must raise without waiting for the next candidate poll."""
        with bitbrew._interrupt_guard() as should_stop:
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
            assert should_stop()

    def test_real_sigint_stops_generation_and_removes_partial(
        self, tmp_path: "os.PathLike[str]"
    ) -> None:
        """A genuine SIGINT must abort generation promptly and leave no output.

        This runs the real _chunked_write in a real process: mocking it out is
        what previously hid the fact that the interrupt flag was never polled.
        """
        outfile = os.path.join(str(tmp_path), "big.txt")
        # 26^7 words: cannot complete, so any exit is the interrupt working.
        proc = subprocess.Popen(
            [sys.executable, BITBREW_PY, "-p", "*******", "--charset", "lower",
             "--force", "-o", outfile],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(1.5)  # let it get well into the run
        assert proc.poll() is None, "an 8e9-word run should not finish this fast"

        proc.send_signal(signal.SIGINT)
        try:
            _, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail("SIGINT did not stop generation within 30s")

        assert proc.returncode == 130
        assert "Interrupted" in stderr
        assert not os.path.exists(outfile)
        assert not _sidecars(tmp_path)

    def test_interrupt_returns_130(
        self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]"
    ) -> None:
        """A KeyboardInterrupt out of the writer surfaces as exit code 130."""
        outfile = os.path.join(str(tmp_path), "int.txt")

        def raise_interrupt(words, file_obj, chunk_size, progress=None, should_stop=None):
            raise KeyboardInterrupt

        with mock.patch("bitbrew._chunked_write", side_effect=raise_interrupt):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])

        assert ret == 130
        assert "Interrupted" in capsys.readouterr().err
        assert not os.path.exists(outfile)
        assert not _sidecars(tmp_path)

    def test_interrupt_restores_old_handler(self, tmp_path: "os.PathLike[str]") -> None:
        """After interrupt handling, the original SIGINT handler is restored."""
        outfile = os.path.join(str(tmp_path), "restore.txt")
        old_handler = signal.getsignal(signal.SIGINT)

        def raise_interrupt(words, file_obj, chunk_size, progress=None, should_stop=None):
            raise KeyboardInterrupt

        with mock.patch("bitbrew._chunked_write", side_effect=raise_interrupt):
            main(["-p", "a*", "--charset", "xy", "-o", outfile])

        assert signal.getsignal(signal.SIGINT) is old_handler


class TestStdoutFailureModes:
    """stdout must report failure as clearly as the -o path does.

    These previously all returned 0: an interrupted run looked successful, and
    a failed write escaped as a raw traceback.
    """

    @staticmethod
    def _pipeline_raising(exc: BaseException):
        """Build a _build_pipeline stand-in that fails after one word."""

        def fake(cfg, should_stop=None):
            yield "aa"
            raise exc

        return fake

    def test_interrupt_returns_130(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An interrupt mid-stream reports 130, matching the -o path."""
        with mock.patch(
            "bitbrew._build_pipeline",
            side_effect=self._pipeline_raising(KeyboardInterrupt()),
        ):
            ret = main(["-p", "a*", "--charset", "xy"])

        assert ret == 130
        assert "Interrupted" in capsys.readouterr().err

    def test_write_error_returns_1_without_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A full disk under a redirect is a clean error, not a stack trace."""
        with mock.patch(
            "bitbrew._build_pipeline",
            side_effect=self._pipeline_raising(
                OSError(errno.ENOSPC, "No space left on device")
            ),
        ):
            ret = main(["-p", "a*", "--charset", "xy"])

        assert ret == 1
        err = capsys.readouterr().err
        assert "could not write to stdout" in err
        assert "Traceback" not in err

    def test_compressed_write_error_returns_1(
        self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]"
    ) -> None:
        """The gzip path reports write failures the same way the plain one does."""
        with (
            mock.patch(
                "bitbrew._build_pipeline",
                side_effect=self._pipeline_raising(
                    OSError(errno.ENOSPC, "No space left on device")
                ),
            ),
            open(os.path.join(str(tmp_path), "sink.gz"), "wb") as sink,
            mock.patch.object(sys, "stdout", _BinaryStdout(sink)),
        ):
            ret = main(["-p", "a*", "--charset", "xy", "--compress"])

        assert ret == 1
        assert "could not write to stdout" in capsys.readouterr().err

    def test_real_sigint_on_compressed_stdout(
        self, tmp_path: "os.PathLike[str]"
    ) -> None:
        """An interrupted gzip stream must not report success.

        Returning 0 here let `bitbrew ... > out.gz && use out.gz` proceed on an
        archive that had stopped mid-member.
        """
        target = os.path.join(str(tmp_path), "part.gz")
        with open(target, "wb") as sink:
            proc = subprocess.Popen(
                [sys.executable, BITBREW_PY, "-p", "******", "--charset", "lower",
                 "--force", "--compress"],
                stdout=sink, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(1.5)
            assert proc.poll() is None, "a 3e8-word run should not finish this fast"
            proc.send_signal(signal.SIGINT)
            try:
                _, stderr = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                pytest.fail("SIGINT did not stop the compressed stdout run")

        assert proc.returncode == 130
        assert "Interrupted" in stderr

    def test_broken_pipe_is_success_and_silent(self) -> None:
        """`| head` closing early is what the user asked for, not a failure."""
        reader = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.readline()"],
            stdin=subprocess.PIPE,
        )
        writer = subprocess.Popen(
            [sys.executable, BITBREW_PY, "-p", "*****", "--charset", "lower",
             "--force"],
            stdout=reader.stdin, stderr=subprocess.PIPE, text=True,
        )
        assert reader.stdin is not None
        reader.stdin.close()
        reader.wait(timeout=30)
        try:
            _, stderr = writer.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            writer.kill()
            writer.communicate()
            pytest.fail("a closed downstream pipe did not stop the writer")

        assert writer.returncode == 0
        # The shutdown flush must not re-raise on the closed pipe.
        assert "Exception ignored" not in stderr
        assert "BrokenPipeError" not in stderr


class TestAtomicOutput:
    """Output must appear at its final path only once fully written."""

    def test_oserror_leaves_no_partial_file(
        self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]"
    ) -> None:
        """A mid-write OSError must clean up rather than leave a truncated file."""
        outfile = os.path.join(str(tmp_path), "words.txt")

        def boom(words, file_obj, chunk_size, progress=None, should_stop=None):
            file_obj.write("aa\nbb\n")
            raise OSError("disk full")

        with mock.patch("bitbrew._chunked_write", side_effect=boom):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])

        assert ret == 1
        assert "could not write" in capsys.readouterr().err
        assert not os.path.exists(outfile), "truncated wordlist left at output path"
        assert not _sidecars(tmp_path)

    def test_success_leaves_no_part_file(self, tmp_path: "os.PathLike[str]") -> None:
        """A successful run renames the sidecar away."""
        outfile = os.path.join(str(tmp_path), "words.txt")
        ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 0
        assert os.path.exists(outfile)
        assert not _sidecars(tmp_path)


class TestDedupPolicy:
    """Deduplication is only paid for when duplicates are actually possible."""

    def test_single_star_pattern_needs_no_dedup(self) -> None:
        assert _needs_dedup(["pass*"]) is False
        assert _needs_dedup(["******"]) is False

    def test_single_optional_pattern_needs_dedup(self) -> None:
        # '??' over 'x' yields 'x' two different ways.
        assert _needs_dedup(["??"]) is True
        assert _needs_dedup(["?x?"]) is True

    def test_multiple_patterns_always_need_dedup(self) -> None:
        assert _needs_dedup(["a*", "b*"]) is True

    def test_optional_pattern_is_still_deduplicated(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Default behaviour still removes the duplicate '?' collapses."""
        ret = main(["-p", "??", "--charset", "x", "--count"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "3"  # "", "x", "xx"

    def test_no_dedup_flag_keeps_duplicates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--no-dedup streams the raw expansion, duplicates included."""
        ret = main(["-p", "??", "--charset", "x", "--count", "--no-dedup"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "4"  # "", "x", "x", "xx"

    def test_no_dedup_warning_below_threshold(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Small deduplicated runs stay quiet."""
        ret = main(["-p", "a*", "-p", "b*", "--charset", "lower", "--count"])
        assert ret == 0
        assert "holds them all in memory" not in capsys.readouterr().err

    def test_dedup_memory_warning_above_threshold(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deduplicated runs past the threshold warn that memory scales with output.

        The threshold is lowered rather than generating a genuinely large run:
        materialising millions of words here would reproduce the very pathology
        the warning exists to flag.
        """
        monkeypatch.setattr("bitbrew._DEDUP_WARN_THRESHOLD", 10)
        ret = main(["-p", "a*", "-p", "b*", "--charset", "lower", "--count"])
        assert ret == 0
        stderr = capsys.readouterr().err
        assert "holds them all in memory" in stderr
        assert "--no-dedup" in stderr

    def test_no_warning_when_dedup_is_skipped(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single '*'-only pattern never deduplicates, so it never warns."""
        monkeypatch.setattr("bitbrew._DEDUP_WARN_THRESHOLD", 10)
        ret = main(["-p", "***", "--charset", "lower", "--count"])
        assert ret == 0
        assert "holds them all in memory" not in capsys.readouterr().err


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
        ret = main(
            ["-p", "**", "--charset", "digits", "--min-len", "2", "--max-len", "2", "--count"]
        )
        assert ret == 0
        output = capsys.readouterr().out.strip()
        assert output == "100"  # 10 * 10

    def test_deduplicate_large_overlapping_patterns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
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

    def test_oserror_during_write(
        self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]"
    ) -> None:
        """OSError during file write should return 1 with a message, not crash."""
        outfile = str(tmp_path / "words.txt")
        with mock.patch("bitbrew.tempfile.mkstemp", side_effect=OSError("disk full")):
            ret = main(["-p", "a*", "--charset", "xy", "-o", outfile])
        assert ret == 1
        assert "could not write" in capsys.readouterr().err
        assert not os.path.exists(outfile)

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


class TestRegexBlowupProbe:
    """The timing probe catches backtracking the structural check misses."""

    @pytest.mark.parametrize(
        "pattern",
        [r"(a|a)+$", r"(a|b|ab)*$", r"(a|aa)+$", r"(a+)+$", r"([a-z]+)*$", r"((a)*)*$"],
    )
    def test_catastrophic_patterns_are_probed_out(self, pattern: str) -> None:
        """Each of these blows up exponentially and must be rejected."""
        assert _probe_regex_blowup(re.compile(pattern), pattern) is not None

    @pytest.mark.parametrize(
        "pattern",
        [r"^abc$", r"\d+", r"[a-z]+", r"(foo|bar)", r"a{2,5}", r"^a.*9$",
         r"(ab+c)+", r"\(a+\)+", r"^[a-z]{3}\d$", r"pass\d+", r"(cat|dog)s?$",
         r"(a|ab)+$", r".*"],
    )
    def test_safe_patterns_pass_the_probe(self, pattern: str) -> None:
        """Linear-time patterns must not be flagged."""
        assert _probe_regex_blowup(re.compile(pattern), pattern) is None

    def test_probe_is_time_bounded(self) -> None:
        """Screening a pathological pattern must itself finish quickly."""
        pattern = r"(a*)*$"
        start = time.perf_counter()
        assert _probe_regex_blowup(re.compile(pattern), pattern) is not None
        assert time.perf_counter() - start < 2.0

    def test_probe_overhead_on_safe_pattern_is_negligible(self) -> None:
        """The common case -- a safe filter -- must not cost real time."""
        start = time.perf_counter()
        assert _probe_regex_blowup(re.compile(r"^a.*9$"), r"^a.*9$") is None
        assert time.perf_counter() - start < 0.05

    def test_alternation_redos_rejected_by_cli(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """(a|a)+$ used to be accepted and would hang; now it is refused."""
        ret = main(["-p", "a*", "--charset", "ab", "--filter", r"(a|a)+$"])
        assert ret == 1
        stderr = capsys.readouterr().err
        assert "unsafe regex" in stderr
        assert "backtracking" in stderr

    def test_escaped_parens_are_not_a_group(self) -> None:
        """'\\(a+\\)+' is literal parens, not a quantified group: no false positive."""
        assert _check_regex_safety(r"\(a+\)+") is None

    def test_allow_unsafe_regex_overrides_static_check(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The screening is a heuristic, so it must be overridable."""
        ret = main(["-p", "a*", "--charset", "xy", "--filter", r"(ab+c)+"])
        assert ret == 1  # false positive from the structural check

        ret = main(["-p", "*at", "--charset", "bcr", "--filter", r"(ca+t)+",
                    "--allow-unsafe-regex"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "cat"


class TestCompressToStdout:
    """--compress used to be silently ignored when writing to stdout."""

    def test_compress_to_pipe_emits_real_gzip(self) -> None:
        """Piping --compress output must yield decompressible gzip data."""
        proc = subprocess.run(
            [sys.executable, BITBREW_PY, "-p", "a*", "--charset", "xy", "--compress"],
            capture_output=True, check=True,
        )
        assert proc.stdout[:2] == b"\x1f\x8b", "not gzip data"
        assert sorted(gzip.decompress(proc.stdout).decode().split()) == ["ax", "ay"]

    def test_compress_to_terminal_is_refused(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binary down a TTY is noise, so it is an error rather than a mess."""
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        ret = main(["-p", "a*", "--charset", "xy", "--compress"])
        assert ret == 1
        assert "refusing to write compressed output to a terminal" in capsys.readouterr().err

    def test_uncompressed_stdout_unaffected(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ret = main(["-p", "a*", "--charset", "xy"])
        assert ret == 0
        assert sorted(capsys.readouterr().out.split()) == ["ax", "ay"]


class TestLengthValidation:
    """Negative lengths used to be accepted and silently emit nothing."""

    def test_negative_max_len_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", "xy", "--max-len", "-1"])
        assert ret == 1
        assert "--max-len must be zero or greater" in capsys.readouterr().err

    def test_negative_min_len_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", "xy", "--min-len", "-5"])
        assert ret == 1
        assert "--min-len must be zero or greater" in capsys.readouterr().err

    def test_zero_min_len_allowed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Zero is a meaningful floor and must still work."""
        ret = main(["-p", "a?", "--charset", "x", "--min-len", "0", "--count"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "2"  # "a", "ax"


class _FakeTqdm:
    """Records the kwargs bitbrew builds its progress bar with."""

    last: "dict[str, object]" = {}

    def __init__(self, **kwargs: object) -> None:
        _FakeTqdm.last = kwargs

    def update(self, n: int) -> None:
        pass

    def close(self) -> None:
        pass


class TestProgressTotal:
    """The bar must not promise a total the run cannot reach."""

    @pytest.fixture(autouse=True)
    def _fake_tqdm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "tqdm", types.SimpleNamespace(tqdm=_FakeTqdm))
        _FakeTqdm.last = {}

    def test_exact_run_gets_a_real_total(self, tmp_path: "os.PathLike[str]") -> None:
        """No filters, no dedup: the estimate is the true count."""
        out = os.path.join(str(tmp_path), "w.txt")
        assert main(["-p", "a*", "--charset", "xy", "-o", out]) == 0
        assert _FakeTqdm.last["total"] == 2

    def test_filtered_run_gets_no_total(self, tmp_path: "os.PathLike[str]") -> None:
        """Filters drop words, so a total would never be reached."""
        out = os.path.join(str(tmp_path), "w.txt")
        assert main(["-p", "a*", "--charset", "xy", "-o", out, "--min-len", "2"]) == 0
        assert _FakeTqdm.last["total"] is None

    def test_deduplicated_run_gets_no_total(self, tmp_path: "os.PathLike[str]") -> None:
        """Dedup can drop words too."""
        out = os.path.join(str(tmp_path), "w.txt")
        assert main(["-p", "a*", "-p", "b*", "--charset", "xy", "-o", out]) == 0
        assert _FakeTqdm.last["total"] is None

    def test_saturated_estimate_gets_no_total(self, tmp_path: "os.PathLike[str]") -> None:
        """A capped estimate is not a real number; show a counter instead."""
        out = os.path.join(str(tmp_path), "w.txt")
        with (
            mock.patch("bitbrew.estimate_count", return_value=_MAX_ESTIMATE),
            mock.patch("bitbrew._chunked_write", return_value=0),
        ):
            assert main(["-p", "a*", "--charset", "xy", "-o", out, "--force"]) == 0
        assert _FakeTqdm.last["total"] is None


class TestInstalledConsoleScript:
    """The packaged entry point has broken twice before with no test to catch it."""

    def test_console_script_runs(self) -> None:
        exe = shutil.which("bitbrew")
        if exe is None:
            pytest.skip("bitbrew is not installed in this environment")
        proc = subprocess.run([exe, "-p", "a*", "--charset", "xy"],
                              capture_output=True, text=True, check=True)
        assert sorted(proc.stdout.split()) == ["ax", "ay"]

    def test_console_script_count(self) -> None:
        exe = shutil.which("bitbrew")
        if exe is None:
            pytest.skip("bitbrew is not installed in this environment")
        proc = subprocess.run([exe, "-p", "**", "--charset", "ab", "--count"],
                              capture_output=True, text=True, check=True)
        assert proc.stdout.strip() == "4"


class TestPatternEscapes:
    """A backslash makes the next character literal."""

    def test_escaped_star_is_literal(self) -> None:
        assert list(_expand_pattern(r"a\*b", "xy")) == ["a*b"]

    def test_escaped_question_is_literal(self) -> None:
        assert list(_expand_pattern(r"a\?b", "xy")) == ["a?b"]

    def test_escaped_backslash_is_one_backslash(self) -> None:
        assert list(_expand_pattern(r"a\\b", "xy")) == ["a\\b"]

    def test_escape_and_wildcard_mix(self) -> None:
        assert list(_expand_pattern(r"\**", "xy")) == ["*x", "*y"]

    def test_dangling_backslash_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="dangling backslash"):
            _parse_pattern("abc\\")

    def test_estimate_ignores_escaped_wildcards(self) -> None:
        """An escaped '*' contributes no combinations."""
        assert estimate_count(r"a\*b", 26) == 1
        assert estimate_count(r"a*b", 26) == 26
        assert estimate_count(r"a\*b*", 26) == 26

    def test_dedup_policy_ignores_escaped_optional(self) -> None:
        """'\\?' is a literal, so it cannot collapse two ways."""
        assert _needs_dedup([r"a\?b"]) is False
        assert _needs_dedup([r"a?b"]) is True

    def test_cli_emits_literal_wildcard(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", r"pw\*", "--charset", "digits"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "pw*"

    def test_cli_rejects_dangling_backslash(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ret = main(["-p", "abc\\", "--charset", "xy"])
        assert ret == 1
        assert "dangling backslash" in capsys.readouterr().err

    def test_escaped_pattern_counts_as_literal_for_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A fully escaped pattern has no wildcards and should say so."""
        ret = main(["-p", r"\*\?", "--charset", "xy"])
        assert ret == 0
        assert "no wildcards" in capsys.readouterr().err


class TestCharsetFile:
    """--charset-file takes characters verbatim, unlike --charset."""

    def test_comma_and_space_survive(self, tmp_path: "os.PathLike[str]") -> None:
        """Both are unreachable through --charset."""
        path = os.path.join(str(tmp_path), "cs.txt")
        with open(path, "w") as handle:
            handle.write("ab, c")
        assert load_charset_file(path) == "ab, c"

    def test_single_trailing_newline_ignored(self, tmp_path: "os.PathLike[str]") -> None:
        path = os.path.join(str(tmp_path), "cs.txt")
        with open(path, "w") as handle:
            handle.write("abc\n")
        assert load_charset_file(path) == "abc"

    def test_duplicates_removed(self, tmp_path: "os.PathLike[str]") -> None:
        path = os.path.join(str(tmp_path), "cs.txt")
        with open(path, "w") as handle:
            handle.write("aabbc")
        assert load_charset_file(path) == "abc"

    def test_cli_uses_the_file(
        self, capsys: pytest.CaptureFixture[str], tmp_path: "os.PathLike[str]"
    ) -> None:
        path = os.path.join(str(tmp_path), "cs.txt")
        with open(path, "w") as handle:
            handle.write("a,")
        ret = main(["-p", "*", "--charset-file", path])
        assert ret == 0
        assert capsys.readouterr().out.split("\n")[:2] == ["a", ","]

    def test_conflicts_with_charset(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "*", "--charset", "lower", "--charset-file", "/nope"])
        assert ret == 1
        assert "mutually exclusive" in capsys.readouterr().err

    def test_missing_file_reports_clearly(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ret = main(["-p", "*", "--charset-file", "/no/such/charset.txt"])
        assert ret == 1
        assert "could not read --charset-file" in capsys.readouterr().err


class TestLimit:
    """--limit samples a pattern space without generating all of it."""

    def test_limit_truncates_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "***", "--charset", "lower", "--limit", "4"])
        assert ret == 0
        assert capsys.readouterr().out.split() == ["aaa", "aab", "aac", "aad"]

    def test_limit_above_available_is_harmless(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ret = main(["-p", "a*", "--charset", "xy", "--limit", "99"])
        assert ret == 0
        assert sorted(capsys.readouterr().out.split()) == ["ax", "ay"]

    def test_limit_must_be_positive(self, capsys: pytest.CaptureFixture[str]) -> None:
        ret = main(["-p", "a*", "--charset", "xy", "--limit", "0"])
        assert ret == 1
        assert "--limit must be greater than 0" in capsys.readouterr().err

    def test_limit_lets_a_huge_space_be_sampled(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A limited sample of a 26^7 space must not need --force or real time."""
        ret = main(["-p", "*******", "--charset", "lower", "--limit", "3"])
        assert ret == 0
        assert capsys.readouterr().out.split() == ["aaaaaaa", "aaaaaab", "aaaaaac"]


class TestAnalyticCount:
    """--count answers from arithmetic when nothing can drop a word."""

    def test_huge_count_is_instant_and_needs_no_force(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        start = time.perf_counter()
        ret = main(["-p", "*******", "--charset", "lower", "--count"])
        elapsed = time.perf_counter() - start
        assert ret == 0
        assert capsys.readouterr().out.strip() == str(26**7)
        assert elapsed < 1.0, "count should not enumerate"

    def test_analytic_count_matches_enumeration(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The shortcut must agree with actually counting."""
        ret = main(["-p", "***", "--charset", "abc", "--count"])
        assert ret == 0
        analytic = capsys.readouterr().out.strip()
        ret = main(["-p", "***", "--charset", "abc"])
        assert ret == 0
        assert analytic == str(len(capsys.readouterr().out.split()))

    def test_filters_force_enumeration(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With a filter the estimate is only an upper bound."""
        ret = main(["-p", "a?", "--charset", "xy", "--min-len", "2", "--count"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "2"

    def test_dedup_forces_enumeration(self, capsys: pytest.CaptureFixture[str]) -> None:
        """'??' collapses, so arithmetic would overcount."""
        ret = main(["-p", "??", "--charset", "x", "--count"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "3"

    def test_count_with_limit_is_analytic(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ret = main(["-p", "*******", "--charset", "lower", "--limit", "5", "--count"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "5"

    def test_generation_still_needs_force(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Counting is free, but actually emitting 8e9 words is not."""
        ret = main(["-p", "*******", "--charset", "lower", "--count", "--no-dedup"])
        assert ret == 0
        ret = main(["-p", "*******", "--charset", "lower"])
        assert ret == 1
        assert "Use --force to proceed" in capsys.readouterr().err


class TestBloomFilter:
    """Approximate dedup trades a quantified error rate for bounded memory."""

    @pytest.mark.parametrize("target", [1e-2, 1e-3])
    def test_measured_error_rate_tracks_the_target(self, target: float) -> None:
        """The whole feature rests on this number being honest."""
        count = 50_000
        bloom = _BloomFilter(count, target, _BLOOM_MAX_BYTES)
        for i in range(count):
            bloom.add_if_absent(f"item{i}")
        # Query-only: adding the probes would load the filter past its capacity.
        probes = 50_000
        false_positives = sum(
            1 for i in range(count, count + probes)
            if bloom.probably_contains(f"item{i}")
        )
        measured = false_positives / probes
        assert measured < target * 4, f"measured {measured:.2e} against target {target:.0e}"

    def test_no_false_negatives(self) -> None:
        """A word that was added is never reported absent -- that direction is exact."""
        bloom = _BloomFilter(1000, 1e-3, _BLOOM_MAX_BYTES)
        words = [f"w{i}" for i in range(1000)]
        for word in words:
            bloom.add_if_absent(word)
        assert all(bloom.probably_contains(word) for word in words)

    def test_add_if_absent_reports_first_sight(self) -> None:
        bloom = _BloomFilter(100, 1e-3, _BLOOM_MAX_BYTES)
        assert bloom.add_if_absent("hello") is True
        assert bloom.add_if_absent("hello") is False

    def test_memory_is_capped(self) -> None:
        """A saturated estimate must not try to allocate the ideal size."""
        bloom = _BloomFilter(10**12, 1e-9, max_bytes=1 << 20)
        assert bloom.size_bytes <= 1 << 20
        # The reported rate degrades honestly rather than staying at the target.
        assert bloom.expected_error_rate > 1e-9

    def test_capped_filter_really_does_drop_valid_words(self) -> None:
        """Pin down what a pinned error rate costs, so the refusal has a reason.

        This test previously asserted only that the rate rose above the target,
        which a rate of exactly 1.0 satisfies -- it accepted total data loss as
        correct behaviour.
        """
        bloom = _BloomFilter(10**6, 1e-6, max_bytes=1024)
        assert bloom.expected_error_rate == pytest.approx(1.0)
        kept = sum(1 for i in range(5000) if bloom.add_if_absent(f"distinct-{i}"))
        assert kept < 5000 * 0.9, "a rate of 1.0 should be losing most words"

    def test_sizing_does_not_allocate(self) -> None:
        """_plan_bloom must be answerable without paying for the bit array."""
        plan = _plan_bloom(10**15, 1e-9, _BLOOM_MAX_BYTES)
        assert plan.size_bytes == _BLOOM_MAX_BYTES
        assert plan.error_rate == pytest.approx(1.0)

    def test_run_is_refused_when_the_rate_cannot_be_honoured(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Rather than silently discarding most of the wordlist, stop."""
        with mock.patch("bitbrew._BLOOM_MAX_BYTES", 64):
            ret = main(["-p", "??????", "-p", "??????", "--charset", "lower",
                        "--force", "--dedup-approx", "--count"])

        assert ret == 1
        err = capsys.readouterr().err
        assert "approximate deduplication" in err
        assert "--no-dedup" in err, "the error must name a way forward"

    def test_run_is_refused_for_any_worse_rate(self) -> None:
        """The memory cap may not silently spend a multiple of the requested rate."""
        requested = 1e-6
        capacity = 700_000_000
        plan = _plan_bloom(capacity, requested, _BLOOM_MAX_BYTES)
        assert requested < plan.error_rate < requested * 10

        cfg = types.SimpleNamespace(
            dedup="approx", dedup_capacity=capacity, dedup_error=requested
        )
        with pytest.raises(bitbrew._CliError, match="best rate"):
            bitbrew._dedup_stage(iter(()), cfg)

    def test_refusal_omits_dedup_error_advice_when_useless(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--dedup-error only takes values below 1, so never suggest 1."""
        with mock.patch("bitbrew._BLOOM_MAX_BYTES", 64):
            main(["-p", "??????", "-p", "??????", "--charset", "lower",
                  "--force", "--dedup-approx", "--count"])

        err = capsys.readouterr().err
        assert "--dedup-error 1 " not in err
        assert "--dedup-error 1\n" not in err

    def test_suggested_dedup_error_is_actually_usable(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Advice the parser would reject is worse than no advice.

        Takes the rate named in the refusal and feeds it straight back, which
        must clear both the --dedup-error range check and the refusal itself.
        """
        argv = ["-p", "????", "-p", "????", "--charset", "lower",
                "--dedup-approx", "--dedup-error", "1e-9", "--count"]
        with mock.patch("bitbrew._BLOOM_MAX_BYTES", 1_900_000):
            assert main(argv) == 1
            err = capsys.readouterr().err
            match = re.search(r"--dedup-error (\S+) to accept", err)
            assert match, f"no usable rate suggested in: {err}"

            retry = argv.copy()
            retry[retry.index("1e-9")] = match.group(1)
            assert main(retry) == 0, "the tool suggested a rate it then rejects"

    def test_achievable_rate_is_accepted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A run whose requested rate does fit must still go through untouched."""
        ret = main(["-p", "a*", "-p", "a*", "--charset", "lower",
                    "--dedup-approx", "--count"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "26"

    def test_memory_error_becomes_a_clean_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An oversized allocation must not surface as a bare MemoryError."""
        with mock.patch("bitbrew.bytearray", side_effect=MemoryError, create=True):
            ret = main(["-p", "a*", "-p", "a*", "--charset", "lower",
                        "--dedup-approx", "--count"])

        assert ret == 1
        assert "not enough memory" in capsys.readouterr().err

    def test_cli_dedup_approx_removes_duplicates(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ret = main(["-p", "a*", "-p", "a*", "--charset", "lower",
                    "--dedup-approx", "--count"])
        assert ret == 0
        assert capsys.readouterr().out.strip() == "26"

    def test_cli_dedup_approx_announces_its_cost(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["-p", "a*", "-p", "b*", "--charset", "lower", "--dedup-approx", "--count"])
        stderr = capsys.readouterr().err
        assert "approximate deduplication" in stderr
        assert "may be dropped" in stderr

    def test_dedup_flags_are_mutually_exclusive(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ret = main(["-p", "a*", "-p", "b*", "--charset", "xy",
                    "--no-dedup", "--dedup-approx"])
        assert ret == 1
        assert "mutually exclusive" in capsys.readouterr().err

    @pytest.mark.parametrize("rate", ["0", "1", "-0.5", "2"])
    def test_error_rate_must_be_a_probability(
        self, capsys: pytest.CaptureFixture[str], rate: str
    ) -> None:
        ret = main(["-p", "a*", "-p", "b*", "--charset", "xy",
                    "--dedup-approx", "--dedup-error", rate])
        assert ret == 1
        assert "--dedup-error must be between 0 and 1" in capsys.readouterr().err


class TestRunConfig:
    """Option resolution is now a unit worth testing directly."""

    def _config(self, *argv: str) -> object:
        return _resolve_options(build_parser().parse_args(list(argv)))

    def test_plain_run_has_an_exact_count(self) -> None:
        cfg = self._config("-p", "***", "--charset", "abc")
        assert cfg.exact_output_count == 27

    def test_filtered_run_has_no_exact_count(self) -> None:
        cfg = self._config("-p", "***", "--charset", "abc", "--min-len", "2")
        assert cfg.exact_output_count is None

    def test_dedup_run_has_no_exact_count(self) -> None:
        cfg = self._config("-p", "a*", "-p", "b*", "--charset", "abc")
        assert cfg.exact_output_count is None

    def test_limit_caps_the_exact_count(self) -> None:
        cfg = self._config("-p", "***", "--charset", "abc", "--limit", "5")
        assert cfg.exact_output_count == 5

    def test_gz_extension_implies_compression(self) -> None:
        cfg = self._config("-p", "a*", "--charset", "xy", "-o", "/tmp/x.gz")
        assert cfg.use_compress is True

    def test_dedup_mode_selection(self) -> None:
        assert self._config("-p", "a*", "--charset", "xy").dedup == "none"
        assert self._config("-p", "a?", "--charset", "xy").dedup == "exact"
        assert self._config("-p", "a*", "-p", "b*", "--charset", "xy").dedup == "exact"
        assert self._config(
            "-p", "a*", "-p", "b*", "--charset", "xy", "--dedup-approx"
        ).dedup == "approx"
        assert self._config(
            "-p", "a*", "-p", "b*", "--charset", "xy", "--no-dedup"
        ).dedup == "none"


class TestVersionFlag:
    def test_version_prints_and_exits_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0
        assert capsys.readouterr().out.strip() == f"bitbrew {bitbrew.__version__}"

    def test_version_matches_package_metadata(self) -> None:
        """pyproject reads the version from the module, so they cannot drift."""
        installed = importlib.metadata.version("bitbrew")
        assert installed == bitbrew.__version__


class TestReviewFindings:
    """Regressions for defects found in review of this branch."""

    def test_limit_with_filter_still_needs_force(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--limit bounds emitted words, not words examined.

        A filter that matches nothing walks the whole space regardless of the
        limit, so the guard has to keep asking about the full estimate.
        """
        ret = main(["-p", "*******", "--charset", "lower",
                    "--filter", "^Z$", "--limit", "1"])
        assert ret == 1
        assert "Use --force to proceed" in capsys.readouterr().err

    def test_limit_with_dedup_still_needs_force(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Deduplication consumes candidates the limit never sees."""
        ret = main(["-p", "a******", "-p", "b******", "--charset", "lower",
                    "--limit", "1"])
        assert ret == 1
        assert "Use --force to proceed" in capsys.readouterr().err

    def test_bare_limit_still_skips_the_guard(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """With nothing discarding candidates, the limit does bound the work."""
        ret = main(["-p", "*******", "--charset", "lower", "--limit", "3"])
        assert ret == 0
        assert capsys.readouterr().out.split() == ["aaaaaaa", "aaaaaab", "aaaaaac"]

    def test_bloom_is_sized_for_a_limited_run(self) -> None:
        """A 3-word sample must not allocate a filter for the whole space."""
        argv = ["-p", "a*******", "-p", "b*******", "--charset", "lower",
                "--limit", "3", "--dedup-approx"]
        cfg = _resolve_options(build_parser().parse_args(argv))
        assert cfg.total_estimate > 10**9
        assert cfg.dedup_capacity == 3
        bloom = _BloomFilter(cfg.dedup_capacity, cfg.dedup_error, _BLOOM_MAX_BYTES)
        assert bloom.size_bytes < 1024, "filter sized for the space, not the sample"

    def test_bloom_capacity_respects_limit_even_when_filtering(self) -> None:
        """Filters now sit upstream of dedup, so they cannot inflate it.

        This asserted the opposite while filters ran after dedup. Sizing for
        the whole space under a limit is not merely wasteful now: it can push
        the filter past its ceiling and get an otherwise fine run refused.
        """
        argv = ["-p", "a***", "-p", "b***", "--charset", "lower",
                "--limit", "3", "--filter", "^zzz", "--dedup-approx"]
        cfg = _resolve_options(build_parser().parse_args(argv))
        assert cfg.total_estimate > 3
        assert cfg.dedup_capacity == 3

    def test_dedup_never_tracks_more_than_the_limit(self) -> None:
        """The capacity bound above must hold in the built pipeline, not just on paper.

        A filter that rejects almost everything is the case that would break it:
        dedup must still never be handed more distinct words than --limit.
        """
        argv = ["-p", "a***", "-p", "b***", "--charset", "lower",
                "--limit", "3", "--filter", "^a.a", "--dedup-approx"]
        cfg = _resolve_options(build_parser().parse_args(argv))

        peak = 0
        real_add = bitbrew._BloomFilter.add_if_absent
        held = set()

        def counting_add(self, item: str) -> bool:
            nonlocal peak
            fresh = real_add(self, item)
            if fresh:
                held.add(item)
                peak = max(peak, len(held))
            return fresh

        with mock.patch.object(bitbrew._BloomFilter, "add_if_absent", counting_add):
            words = list(bitbrew._build_pipeline(cfg))

        assert len(words) == 3
        assert peak <= 3, f"dedup held {peak} words under --limit 3"

    def test_existing_sidecar_is_not_destroyed(
        self, tmp_path: "os.PathLike[str]"
    ) -> None:
        """A fixed sidecar name would truncate an unrelated leftover file."""
        outfile = os.path.join(str(tmp_path), "words.txt")
        bystander = outfile + ".part"
        with open(bystander, "w") as handle:
            handle.write("irreplaceable")

        assert main(["-p", "a*", "--charset", "xy", "-o", outfile]) == 0

        with open(bystander) as handle:
            assert handle.read() == "irreplaceable", "clobbered a pre-existing file"

    def test_concurrent_runs_do_not_share_a_sidecar(
        self, tmp_path: "os.PathLike[str]"
    ) -> None:
        """Two runs targeting one output must not collide on the temp path."""
        outfile = os.path.join(str(tmp_path), "words.txt")
        seen: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def recording_mkstemp(*args: object, **kwargs: object):
            handle, path = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
            seen.append(path)
            return handle, path

        with mock.patch("bitbrew.tempfile.mkstemp", side_effect=recording_mkstemp):
            assert main(["-p", "a*", "--charset", "xy", "-o", outfile]) == 0
            assert main(["-p", "b*", "--charset", "xy", "-o", outfile,
                         "--overwrite"]) == 0
        assert len(seen) == 2
        assert seen[0] != seen[1], "both runs used the same sidecar path"

    def test_output_is_world_readable(self, tmp_path: "os.PathLike[str]") -> None:
        """mkstemp creates 0600; the finished file must not inherit that."""
        outfile = os.path.join(str(tmp_path), "words.txt")
        assert main(["-p", "a*", "--charset", "xy", "-o", outfile]) == 0
        mode = stat.S_IMODE(os.stat(outfile).st_mode)
        expected = 0o666 & ~bitbrew._current_umask()
        assert mode == expected, f"got {mode:o}, expected {expected:o}"


class TestReadmeExamples:
    """The README's examples must do what the README says they do.

    The escape example shipped as `-p 'pw\\*'`, which is correct inside the
    module docstring -- where \\ renders as a single backslash -- but wrong in
    Markdown, where it reaches the shell verbatim. It emitted ten words
    containing a literal backslash instead of the one word documented.
    """

    README = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")

    def _readme(self) -> str:
        with open(self.README, encoding="utf-8") as handle:
            return handle.read()

    def test_documented_escape_example_emits_one_literal_word(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Run the escape example exactly as the README prints it."""
        text = self._readme()
        match = re.search(r"^bitbrew -p '(pw[^']*)' --charset digits$", text, re.M)
        assert match, "the documented escape example has moved or changed shape"

        assert main(["-p", match.group(1), "--charset", "digits"]) == 0
        assert capsys.readouterr().out.split() == ["pw*"]

    def test_escape_is_documented_as_a_single_backslash(self) -> None:
        """A doubled backslash in Markdown is a different, working pattern."""
        text = self._readme()
        assert "`\\\\`" not in text, "Markdown renders \\\\ literally, not as one backslash"

    def test_sidecar_name_is_documented_accurately(
        self, tmp_path: "os.PathLike[str]"
    ) -> None:
        """mkstemp inserts a random component, so `<output>.part` is not the name."""
        assert "`<output>.<random>.part`" in self._readme()

        outfile = os.path.join(str(tmp_path), "words.txt")
        seen: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def recording_mkstemp(*args: object, **kwargs: object):
            handle, path = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
            seen.append(os.path.basename(path))
            return handle, path

        with mock.patch("bitbrew.tempfile.mkstemp", side_effect=recording_mkstemp):
            assert main(["-p", "a*", "--charset", "xy", "-o", outfile]) == 0

        assert len(seen) == 1
        assert seen[0] != "words.txt.part", "the documented name was the real one"
        assert re.fullmatch(r"words\.txt\.\w+\.part", seen[0]), seen[0]
