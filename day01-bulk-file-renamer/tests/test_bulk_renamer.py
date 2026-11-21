"""Tests for bulk_renamer — run with:  python -m unittest discover tests"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bulk_renamer as br  # noqa: E402


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = br.main([str(a) for a in argv])
    return code, out.getvalue() + err.getvalue()


class RenamerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, *names):
        for n in names:
            p = self.dir / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(n)

    def names(self, sub=""):
        base = self.dir / sub if sub else self.dir
        return sorted(p.name for p in base.iterdir() if p.is_file() and not br.is_log_file(p.name))

    def test_template_numbering_natural_order(self):
        self.touch("img10.jpg", "img2.jpg", "img1.jpg")
        code, _ = run(self.dir, "-t", "trip_{n:03}", "-y", "--no-progress")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), ["trip_001.jpg", "trip_002.jpg", "trip_003.jpg"])
        self.assertEqual((self.dir / "trip_003.jpg").read_text(), "img10.jpg")

    def test_replace_case_and_ext(self):
        self.touch("My Holiday Photo.JPG")
        code, _ = run(self.dir, "--case", "snake", "--lower-ext", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), ["my_holiday_photo.jpg"])

    def test_regex_and_prefix_suffix(self):
        self.touch("20260115_report.pdf")
        code, _ = run(self.dir, "--regex", r"^(\d{4})(\d{2})(\d{2})", r"\1-\2-\3", "--prefix", "Q1_",
                      "--suffix", "_final", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), ["Q1_2026-01-15_report_final.pdf"])

    def test_dry_run_changes_nothing(self):
        self.touch("a.txt", "b.txt")
        code, out = run(self.dir, "--prefix", "x_", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(self.names(), ["a.txt", "b.txt"])

    def test_conflict_aborts_without_changes(self):
        self.touch("a.txt", "b.txt")
        code, out = run(self.dir, "-t", "same", "-y")
        self.assertEqual(code, 1)
        self.assertIn("conflict", out)
        self.assertEqual(self.names(), ["a.txt", "b.txt"])

    def test_conflict_with_existing_file_skip(self):
        self.touch("a.txt", "x_a.txt", "b.txt")
        code, _ = run(self.dir, "-g", "?.txt", "--prefix", "x_", "--on-conflict", "skip", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), ["a.txt", "x_a.txt", "x_b.txt"])

    def test_conflict_suffix(self):
        self.touch("a.txt", "b.txt", "c.txt")
        code, _ = run(self.dir, "-t", "doc", "--on-conflict", "suffix", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), ["doc (1).txt", "doc (2).txt", "doc.txt"])

    def test_swap_names_two_phase(self):
        self.touch("1.txt", "2.txt")
        br.execute([(self.dir / "1.txt", self.dir / "2.txt"), (self.dir / "2.txt", self.dir / "1.txt")],
                   show_progress=False)
        self.assertEqual((self.dir / "1.txt").read_text(), "2.txt")
        self.assertEqual((self.dir / "2.txt").read_text(), "1.txt")

    def test_chain_rename_via_template(self):
        # file_1 -> file_2 while file_2 -> file_3: must not clobber
        self.touch("file_1.txt", "file_2.txt")
        code, _ = run(self.dir, "-t", "file_{n}", "--start", "2", "-y")
        self.assertEqual(code, 0)
        self.assertEqual((self.dir / "file_2.txt").read_text(), "file_1.txt")
        self.assertEqual((self.dir / "file_3.txt").read_text(), "file_2.txt")

    def test_undo_roundtrip(self):
        self.touch("one.txt", "two.txt")
        self.assertEqual(run(self.dir, "--case", "upper", "-y")[0], 0)
        self.assertEqual(self.names(), ["ONE.txt", "TWO.txt"])
        code, _ = run(self.dir, "--undo", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), ["one.txt", "two.txt"])
        self.assertTrue(any(p.name.endswith(br.UNDONE_SUFFIX) for p in self.dir.iterdir()))
        # nothing left to undo now
        self.assertEqual(run(self.dir, "--undo", "-y")[0], 1)

    def test_recursive_and_hidden(self):
        self.touch("top.txt", "sub/inner.txt", ".secret.txt", ".git/config.txt")
        code, _ = run(self.dir, "-r", "--prefix", "p_", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), [".secret.txt", "p_top.txt"])
        self.assertEqual(self.names("sub"), ["p_inner.txt"])
        self.assertEqual(self.names(".git"), ["config.txt"])

    def test_ignore_case_glob(self):
        self.touch("A.JPG", "b.jpg", "c.png")
        code, _ = run(self.dir, "-g", "*.jpg", "-i", "--prefix", "x_", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), ["c.png", "x_A.JPG", "x_b.jpg"])

    def test_invalid_template_and_regex(self):
        self.touch("a.txt")
        self.assertEqual(run(self.dir, "-t", "{nope}", "-y")[0], 1)
        self.assertEqual(run(self.dir, "-t", "{}", "-y")[0], 1)
        self.assertEqual(run(self.dir, "-t", "{name.__class__}", "-y")[0], 1)
        self.assertEqual(run(self.dir, "--regex", "(", "x", "-y")[0], 1)
        self.assertEqual(self.names(), ["a.txt"])

    def test_invalid_chars_sanitised(self):
        self.touch("a.txt")
        code, _ = run(self.dir, "--replace", "a", "x:y?", "-y")
        self.assertEqual(code, 0)
        self.assertEqual(self.names(), ["x_y_.txt"])

    def test_empty_name_rejected(self):
        self.touch("abc.txt")
        code, out = run(self.dir, "--replace", "abc", "", "-y")
        self.assertEqual(code, 0)
        self.assertIn("empty", out)
        self.assertEqual(self.names(), ["abc.txt"])

    def test_missing_directory_and_no_options(self):
        self.assertEqual(run(self.dir / "nope", "--prefix", "x")[0], 1)
        self.assertEqual(run(self.dir)[0], 1)

    def test_non_interactive_requires_yes(self):
        self.touch("a.txt")
        stdin = sys.stdin
        sys.stdin = io.StringIO("")
        try:
            code, out = run(self.dir, "--prefix", "x_")
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 1)
        self.assertIn("--yes", out)
        self.assertEqual(self.names(), ["a.txt"])

    def test_log_contents(self):
        self.touch("a.txt")
        run(self.dir, "--prefix", "z_", "-y")
        log = next(p for p in self.dir.iterdir() if br.is_log_file(p.name))
        data = json.loads(log.read_text())
        self.assertEqual(Path(data["operations"][0]["to"]).name, "z_a.txt")

    def test_rollback_on_failure(self):
        self.touch("a.txt", "b.txt")
        ops = [(self.dir / "a.txt", self.dir / "a2.txt"), (self.dir / "b.txt", self.dir / "nodir" / "b2.txt")]
        with self.assertRaises(br.RenameError) as ctx:
            br.execute(ops, show_progress=False)
        self.assertIn("restored", str(ctx.exception))
        self.assertEqual(self.names(), ["a.txt", "b.txt"])


if __name__ == "__main__":
    unittest.main()
