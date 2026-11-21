import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import maintain  # noqa: E402

DONE = '[12:00:00] [Server thread/INFO]: Done (1.234s)! For help, type "help" or "?"'


class IgnoreFileTest(unittest.TestCase):
    def test_patterns_skip_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / ".github").mkdir()
            (project / maintain.IGNORE_FILE).write_text(
                "# held back\ncom.github.GTNewHorizons:Angelica\n\ncurse.maven:*  # all of CurseMaven\n")
            patterns = maintain.read_ignore(project)
        self.assertEqual(["com.github.GTNewHorizons:Angelica", "curse.maven:*"], patterns)
        self.assertTrue(maintain.ignored("com.github.GTNewHorizons:Angelica", patterns))
        self.assertTrue(maintain.ignored("curse.maven:archaicfix-677782", patterns))
        self.assertFalse(maintain.ignored("com.github.GTNewHorizons:Hodgepodge", patterns))

    def test_minecrafts_lwjgl_is_never_bumped(self):
        self.assertTrue(maintain.ignored("org.lwjgl.lwjgl:lwjgl", maintain.BUILTIN_IGNORES))

    def test_no_file_ignores_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual([], maintain.read_ignore(Path(folder)))


class ServerCheckTest(unittest.TestCase):
    def test_clean_start_and_stop_passes(self):
        self.assertIsNone(maintain.server_problem(DONE + "\nStopping server", []))

    def test_unfinished_start_fails(self):
        self.assertEqual("the server did not finish starting", maintain.server_problem("Loading...", []))

    def test_fatal_errors_fail(self):
        self.assertEqual("fatal errors were detected",
                         maintain.server_problem(DONE + "\nFatal errors were detected during the transition", []))

    def test_crash_report_fails_with_its_text(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "crash-2026-11-21_12.00.00-server.txt"
            report.write_text("java.lang.NoClassDefFoundError: Foo")
            problem = maintain.server_problem(DONE, [report])
        self.assertIn("NoClassDefFoundError", problem)


class ServerPortTest(unittest.TestCase):
    def test_port_replaces_an_existing_one_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as folder:
            properties = Path(folder) / "server.properties"
            properties.write_text("online-mode=true\nserver-port=25565\n")
            maintain.set_server_port(properties, 31000)
            self.assertEqual("online-mode=true\nserver-port=31000\n", properties.read_text())

    def test_free_port_is_usable(self):
        self.assertGreater(maintain.free_port(), 0)


class IsolationTest(unittest.TestCase):
    """test_bumps with the checks faked: a set of bumps fails when it contains a broken one."""

    def setUp(self):
        self.applied = []
        self.saved = (maintain.apply, maintain.checks, maintain.git)
        maintain.apply = lambda project, tools, state, bumps: self.applied.append(list(bumps)) or list(bumps)
        maintain.git = lambda project, *args: self.applied.append([]) or ""

    def tearDown(self):
        maintain.apply, maintain.checks, maintain.git = self.saved

    def fake_checks(self, broken, base_ok=True):
        def checks(project, timeout):
            current = self.applied[-1]
            if not current:
                return maintain.CheckResult(base_ok, "" if base_ok else "dev server", "base")
            bad = [b for b in current if b.coordinate in broken]
            return maintain.CheckResult(not bad, "dev server" if bad else "", "log")
        return checks

    def bumps(self):
        return [maintain.Bump(i, name, "1", "2") for i, name in enumerate(("a:a", "b:b", "c:c"))]

    def test_one_broken_bump_is_split_out(self):
        maintain.checks = self.fake_checks({"b:b"})
        report = maintain.test_bumps(Path("."), Path("."), Path("."), self.bumps(), 300)
        self.assertEqual(["a:a", "c:c"], [b.coordinate for b in report.passing])
        self.assertEqual(["b:b"], [b.coordinate for b, _ in report.failing])
        self.assertIsNone(report.baseline)
        self.assertIsNone(report.together)

    def test_all_passing_needs_one_run(self):
        maintain.checks = self.fake_checks(set())
        report = maintain.test_bumps(Path("."), Path("."), Path("."), self.bumps(), 300)
        self.assertEqual(3, len(report.passing))
        self.assertEqual(1, len(self.applied))

    def test_a_broken_mod_blames_no_bump(self):
        maintain.checks = self.fake_checks({"b:b"}, base_ok=False)
        report = maintain.test_bumps(Path("."), Path("."), Path("."), self.bumps(), 300)
        self.assertIsNotNone(report.baseline)
        self.assertEqual([], report.failing)
        self.assertEqual([], report.passing)


class IssuesOffTest(unittest.TestCase):
    def test_reports_go_to_the_summary(self):
        github = maintain.GitHub.__new__(maintain.GitHub)
        github.project, github.dry_run, github.issues = Path("."), False, False
        with tempfile.TemporaryDirectory() as folder:
            summary = Path(folder) / "summary.md"
            os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
            try:
                github.upsert_issue("dependency update", "a:b", "Updating a:b breaks the build", "the log")
                github.close_issues("dependency update", [], "fixed")
            finally:
                del os.environ["GITHUB_STEP_SUMMARY"]
            self.assertIn("## Updating a:b breaks the build", summary.read_text())


class TextTest(unittest.TestCase):
    def test_issue_names_the_dependency_and_stage(self):
        bump = maintain.Bump(1, "com.github.GTNewHorizons:Angelica", "2.2.17", "2.2.46")
        result = maintain.CheckResult(False, "dev server", "\n".join("line " + str(i) for i in range(200)))
        title, body = maintain.deps_issue(bump, result)
        self.assertEqual("Updating com.github.GTNewHorizons:Angelica to 2.2.46 breaks the dev server", title)
        self.assertIn("line 199", body)
        self.assertNotIn("line 100\n", body)
        self.assertIn(maintain.IGNORE_FILE, body)

    def test_pr_body_lists_every_bump(self):
        body = maintain.deps_pr_body([maintain.Bump(1, "a:b", "1", "2"), maintain.Bump(2, "c:d", "3", "id", "4.0")])
        self.assertIn("| `a:b` | 1 | 2 |", body)
        self.assertIn("| `c:d` | 3 | 4.0 |", body)

    def test_buildscript_version_comes_from_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "settings.gradle.kts").write_text(
                'plugins {\n    id("com.gtnewhorizons.gtnhsettingsconvention") version("2.0.34")\n}\n')
            self.assertEqual("2.0.34", maintain.buildscript_version(project))

    def test_buildscript_version_reads_groovy_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "settings.gradle").write_text(
                "plugins {\n    id 'com.gtnewhorizons.gtnhsettingsconvention' version '1.0.38'\n}\n")
            self.assertEqual("1.0.38", maintain.buildscript_version(project))

    def test_gradle_properties_unescape_colons(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "gradle.properties").write_text(
                "# comment\nmodrinthProjectId = 1viIIPBy\n67minecraftRelations = required-project\\:unimixins\n"
                "curseForgeProjectId =\n")
            values = maintain.gradle_properties(project)
        self.assertEqual("1viIIPBy", values["modrinthProjectId"])
        self.assertEqual("required-project:unimixins", values["67minecraftRelations"])
        self.assertEqual("", values["curseForgeProjectId"])


if __name__ == "__main__":
    unittest.main()
