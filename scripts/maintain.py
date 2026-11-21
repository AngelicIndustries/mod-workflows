#!/usr/bin/env python3
"""Keeps a GTNH-buildscript mod up to date from CI.

    maintain.py deps         bumps dependencies.gradle, opens one PR with the bumps that pass, an issue per one that breaks
    maintain.py buildscript  runs updateBuildScript, opens a PR if it passes, an issue if it breaks
    maintain.py readme       pushes README.md to the Modrinth, 67minecraft and CurseForge pages set in gradle.properties

A bump passes when the mod assembles, builds with its tests and Spotless check, and its dev server starts and stops
cleanly, the checks GTNH's build-and-test runs. Run it from the mod's folder. GitHub work goes through the gh CLI,
with GH_TOKEN set; --dry-run only prints what it would do there.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEPS_FILE = "dependencies.gradle"
IGNORE_FILE = ".github/dependency-updates.ignore"
DEPS_BRANCH = "maintenance/dependencies"
BUILDSCRIPT_BRANCH = "maintenance/buildscript"
DEPS_LABEL = "dependency update"
BUILDSCRIPT_LABEL = "buildscript update"
README_LABEL = "readme sync"
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
LOG_TAIL_LINES = 80
USER_AGENT = "AngelicIndustries/mod-workflows (https://github.com/AngelicIndustries/mod-workflows)"
# CurseForge's authors site sits behind Cloudflare and expects a browser
BROWSER_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


# Checks


@dataclass
class CheckResult:
    ok: bool
    stage: str = ""
    log: str = ""

    def tail(self, lines: int = LOG_TAIL_LINES) -> str:
        return "\n".join(self.log.rstrip().splitlines()[-lines:])


def run(command: List[str], cwd: Path, timeout: Optional[int] = None, stdin: Optional[Path] = None) -> tuple:
    """Runs a command, echoing its output; returns (exit code, output). A timeout counts as exit code 124."""
    print("$ " + " ".join(command), flush=True)
    stdin_handle = open(stdin, "rb") if stdin else subprocess.DEVNULL
    try:
        completed = subprocess.run(
            command, cwd=cwd, stdin=stdin_handle, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        output = completed.stdout.decode("utf-8", "replace")
        code = completed.returncode
    except subprocess.TimeoutExpired as expired:
        output = (expired.stdout or b"").decode("utf-8", "replace")
        code = 124
    finally:
        if stdin:
            stdin_handle.close()
    sys.stdout.write(output)
    sys.stdout.flush()
    return code, output


def server_problem(log: str, crash_reports: List[Path]) -> Optional[str]:
    """Why a dev server run failed, or None when it started and stopped cleanly. Same rules as GTNH's CI."""
    if crash_reports:
        latest = sorted(crash_reports)[-1]
        return "crash report " + latest.name + ":\n" + latest.read_text("utf-8", "replace")
    for text, reason in (
            ("Fatal errors were detected", "fatal errors were detected"),
            ("The state engine was in incorrect state ERRORED and forced into state SERVER_STOPPED",
             "the server was force stopped"),
            ("Exception stopping the server", "the server didn't shut down cleanly")):
        if text in log:
            return reason
    if not re.search(r'Done \(.+\)! For help, type "help" or "\?"', log):
        return "the server did not finish starting"
    return None


def crash_reports(project: Path) -> List[Path]:
    reports: List[Path] = []
    for folder in ("run/crash-reports", "run/server/crash-reports"):
        reports.extend((project / folder).glob("crash*.txt"))
    return reports


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def set_server_port(properties: Path, port: int) -> None:
    """A free port, so another server on the same machine can't fail the check."""
    lines = properties.read_text("utf-8").splitlines() if properties.is_file() else []
    lines = [line for line in lines if not line.startswith("server-port=")] + ["server-port=" + str(port)]
    properties.write_text("\n".join(lines) + "\n")


def checks(project: Path, server_timeout: int) -> CheckResult:
    """Assemble, build with tests, then start and stop the dev server."""
    gradlew = "./gradlew"
    code, output = run([gradlew, "--build-cache", "--stacktrace", "assemble"], project)
    if code != 0:
        return CheckResult(False, "assemble", output)
    build = [gradlew, "--build-cache", "--stacktrace", "build"]
    if shutil.which("xvfb-run"):
        build = ["xvfb-run", "-a", "--server-args=-screen 0 1366x768x24"] + build
    code, output = run(build, project)
    if code != 0:
        return CheckResult(False, "build and tests", output)

    for report in crash_reports(project):
        report.unlink()
    server = project / "run" / "server"
    server.mkdir(parents=True, exist_ok=True)
    port = free_port()
    for folder in (project / "run", server):
        (folder / "eula.txt").write_text("eula=true\n")
        set_server_port(folder / "server.properties", port)
    # Wawel Auth's server module won't start without publicBaseUrl; these are the conventions' dev settings
    wawel = server / "config" / "wawelauth" / "server.json"
    if not wawel.exists():
        wawel.parent.mkdir(parents=True, exist_ok=True)
        wawel.write_text(json.dumps({"publicBaseUrl": "http://localhost:" + str(port) + "/", "enableLocalAuth": False,
                                     "enablePublicPage": False, "enablePublicInfoApi": False}, indent=2) + "\n")
    stop = project / "run" / "stop.txt"
    stop.write_text("stop\n")
    code, output = run([gradlew, "--build-cache", "--stacktrace", "runServer"], project, server_timeout, stop)
    problem = server_problem(output, crash_reports(project))
    if problem:
        return CheckResult(False, "dev server", output + "\n" + problem)
    return CheckResult(True)


# GitHub, through the gh CLI


class GitHub:
    def __init__(self, project: Path, dry_run: bool):
        self.project = project
        self.dry_run = dry_run

    def gh(self, *args: str, read: bool = False) -> str:
        if self.dry_run and not read:
            print("[dry run] gh " + " ".join(args))
            return ""
        completed = subprocess.run(["gh", *args], cwd=self.project, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError("gh " + " ".join(args[:3]) + " failed: " + completed.stderr.strip())
        return completed.stdout

    def ensure_label(self, name: str, color: str) -> None:
        self.gh("label", "create", name, "--color", color, "--force")

    def open_pr(self, branch: str) -> Optional[int]:
        found = json.loads(self.gh("pr", "list", "--head", branch, "--state", "open", "--json", "number",
                                   read=True) or "[]")
        return found[0]["number"] if found else None

    def upsert_pr(self, branch: str, base: str, title: str, body: str, label: str) -> None:
        number = None if self.dry_run else self.open_pr(branch)
        if number:
            self.gh("pr", "edit", str(number), "--title", title, "--body", body)
        else:
            self.gh("pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body,
                    "--label", label)

    def close_pr(self, branch: str, comment: str) -> None:
        number = None if self.dry_run else self.open_pr(branch)
        if number:
            self.gh("pr", "close", str(number), "--comment", comment, "--delete-branch")

    def open_issues(self, label: str) -> List[dict]:
        if self.dry_run:
            return []
        return json.loads(self.gh("issue", "list", "--label", label, "--state", "open", "--limit", "200",
                                  "--json", "number,title,body", read=True) or "[]")

    def upsert_issue(self, label: str, key: str, title: str, body: str) -> None:
        """One open issue per key, marked in its body, so reruns update it instead of opening another."""
        marker = "<!-- mod-workflows:" + key + " -->"
        body = body + "\n\n" + marker
        for issue in self.open_issues(label):
            if marker in (issue.get("body") or ""):
                if issue["title"] != title or issue["body"].strip() != body.strip():
                    self.gh("issue", "edit", str(issue["number"]), "--title", title, "--body", body)
                return
        self.gh("issue", "create", "--title", title, "--body", body, "--label", label)

    def close_issues(self, label: str, keep_keys: List[str], comment: str) -> None:
        """Closes this label's issues whose key isn't in keep_keys."""
        for issue in self.open_issues(label):
            match = re.search(r"<!-- mod-workflows:(.+?) -->", issue.get("body") or "")
            if match and match.group(1) not in keep_keys:
                self.gh("issue", "close", str(issue["number"]), "--comment", comment)


def run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    return server + "/" + repo + "/actions/runs/" + run_id if repo and run_id else "this run"


def git(project: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=project, check=True, capture_output=True, text=True).stdout


def bot_identity() -> tuple:
    """The commit author: the bot GitHub App when the workflow got its token (BOT_SLUG), else github-actions[bot]."""
    slug = os.environ.get("BOT_SLUG", "")
    if not slug:
        return BOT_NAME, BOT_EMAIL
    user = subprocess.run(["gh", "api", "/users/" + slug + "[bot]", "--jq", ".id"], capture_output=True, text=True)
    user_id = user.stdout.strip()
    return slug + "[bot]", (user_id + "+" if user_id else "") + slug + "[bot]@users.noreply.github.com"


def push_branch(project: Path, branch: str, base: str, message: str, files: List[str], dry_run: bool) -> None:
    """Commits the changed files on top of base as branch and force-pushes it; the rolling PR follows."""
    git(project, "checkout", "-B", branch)
    git(project, "add", "--", *files)
    name, email = bot_identity()
    git(project, "-c", "user.name=" + name, "-c", "user.email=" + email, "commit", "-q", "-m", message)
    if dry_run:
        print("[dry run] git push --force origin " + branch)
    else:
        git(project, "push", "--force", "origin", "HEAD:refs/heads/" + branch)
    git(project, "checkout", "-q", base)


# Dependencies


@dataclass
class Bump:
    id: int
    coordinate: str
    current: str
    latest: str
    label: str = ""

    @property
    def shown(self) -> str:
        return self.label or self.latest


@dataclass
class DepsReport:
    passing: List[Bump] = field(default_factory=list)
    failing: List[tuple] = field(default_factory=list)  # (Bump, CheckResult)
    together: Optional[CheckResult] = None  # set when every bump passed alone but not together
    baseline: Optional[CheckResult] = None  # set when the mod fails its checks without any bump


def read_ignore(project: Path) -> List[str]:
    """Patterns from .github/dependency-updates.ignore: group:artifact with * wildcards, # for comments."""
    path = project / IGNORE_FILE
    if not path.is_file():
        return []
    patterns = []
    for line in path.read_text("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            patterns.append(line)
    return patterns


def ignored(coordinate: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatchcase(coordinate, pattern) for pattern in patterns)


def scan(project: Path, tools: Path, state: Path) -> List[Bump]:
    checker = tools / "bin" / "dependency_update_checker.py"
    code, output = run([sys.executable, str(checker), "scan", "--deps-file", str(project / DEPS_FILE),
                        "--state-file", str(state)], project)
    if code != 0:
        raise RuntimeError("the dependency scan failed")
    data = json.loads(state.read_text("utf-8"))
    return [Bump(c["id"], c["group"] + ":" + c["artifact"], c["current_version"], c["latest_version"],
                 c.get("version_label", "")) for c in data.get("candidates", [])]


def apply(project: Path, tools: Path, state: Path, bumps: List[Bump]) -> List[Bump]:
    """Writes these bumps into dependencies.gradle; returns the ones the checker applied (it skips unverifiable ones)."""
    git(project, "checkout", "--", DEPS_FILE)
    checker = tools / "bin" / "dependency_update_checker.py"
    code, output = run([sys.executable, str(checker), "apply", "--state-file", str(state),
                        "--ids", ",".join(str(b.id) for b in bumps)], project)
    if code != 0:
        raise RuntimeError("applying the bumps failed")
    applied = {int(line.split("|")[1]) for line in output.splitlines() if line.startswith("APPLIED|")}
    return [b for b in bumps if b.id in applied]


def test_bumps(project: Path, tools: Path, state: Path, bumps: List[Bump], server_timeout: int) -> DepsReport:
    """All bumps together first; when that fails, each alone, then the passing ones together again."""
    report = DepsReport()
    bumps = apply(project, tools, state, bumps)
    if not bumps:
        return report
    result = checks(project, server_timeout)
    if result.ok:
        report.passing = list(bumps)
        return report
    # Blame the bumps only if the mod passes without them
    git(project, "checkout", "--", DEPS_FILE)
    base = checks(project, server_timeout)
    if not base.ok:
        report.baseline = base
        return report
    if len(bumps) == 1:
        report.failing = [(bumps[0], result)]
        return report
    for bump in bumps:
        apply(project, tools, state, [bump])
        single = checks(project, server_timeout)
        if single.ok:
            report.passing.append(bump)
        else:
            report.failing.append((bump, single))
    if len(report.passing) > 1 and report.failing:
        apply(project, tools, state, report.passing)
        together = checks(project, server_timeout)
        if not together.ok:
            report.together = together
    return report


def baseline_title(result: CheckResult) -> str:
    return "The mod fails its " + result.stage + " check without any update"


def baseline_body(result: CheckResult) -> str:
    return ("Updates are on hold: the mod fails its " + result.stage + " check as it is, so no update can be tested: "
            + run_url() + ".\n\n```\n" + result.tail() + "\n```")


def deps_pr_body(bumps: List[Bump]) -> str:
    rows = "\n".join("| `" + b.coordinate + "` | " + b.current + " | " + b.shown + " |" for b in bumps)
    return ("| Dependency | From | To |\n|---|---|---|\n" + rows + "\n\n"
            "The mod assembled, built with its tests and started its dev server with these: " + run_url() + ".\n\n"
            "Add a dependency to `" + IGNORE_FILE + "` to stop these updates.")


def deps_issue(bump: Bump, result: CheckResult) -> tuple:
    title = "Updating " + bump.coordinate + " to " + bump.shown + " breaks the " + result.stage
    body = ("Updating `" + bump.coordinate + "` from " + bump.current + " to " + bump.shown + " fails the "
            + result.stage + " check: " + run_url() + ".\n\n```\n" + result.tail() + "\n```\n\n"
            "Add `" + bump.coordinate + "` to `" + IGNORE_FILE + "` to stop trying this update.")
    return title, body


def cmd_deps(args: argparse.Namespace) -> int:
    project = Path.cwd()
    github = GitHub(project, args.dry_run)
    base = git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
    state = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "dependency-state.json"
    patterns = read_ignore(project)
    bumps = [b for b in scan(project, Path(args.tools), state) if not ignored(b.coordinate, patterns)]
    report = test_bumps(project, Path(args.tools), state, bumps, args.server_timeout) if bumps else DepsReport()
    git(project, "checkout", "--", DEPS_FILE)

    github.ensure_label(DEPS_LABEL, "0366d6")
    if report.baseline:
        github.upsert_issue(DEPS_LABEL, "baseline", baseline_title(report.baseline), baseline_body(report.baseline))
        github.close_pr(DEPS_BRANCH, "The mod fails its checks without any update, see the open issue.")
        print("the mod fails its checks without any update")
        return 0
    keys = []
    for bump, result in report.failing:
        title, body = deps_issue(bump, result)
        github.upsert_issue(DEPS_LABEL, bump.coordinate, title, body)
        keys.append(bump.coordinate)
    if report.together:
        listed = ", ".join("`" + b.coordinate + "`" for b in report.passing)
        github.upsert_issue(DEPS_LABEL, "together", "Dependency updates pass alone but break the "
                            + report.together.stage + " together",
                            "Each of " + listed + " passes alone, but not all together: " + run_url()
                            + ".\n\n```\n" + report.together.tail() + "\n```")
        keys.append("together")
    github.close_issues(DEPS_LABEL, keys, "This update passes now, or isn't offered anymore.")

    if report.passing and not report.together:
        apply(project, Path(args.tools), state, report.passing)
        title = "Bump " + (report.passing[0].coordinate.split(":")[1] + " to " + report.passing[0].shown
                           if len(report.passing) == 1 else str(len(report.passing)) + " dependencies")
        message = title + "\n\n" + "\n".join("* " + b.coordinate + " " + b.current + " -> " + b.shown
                                             for b in report.passing)
        push_branch(project, DEPS_BRANCH, base, message, [DEPS_FILE], args.dry_run)
        github.upsert_pr(DEPS_BRANCH, base, title, deps_pr_body(report.passing), DEPS_LABEL)
    else:
        github.close_pr(DEPS_BRANCH, "No dependency update passes right now.")
    print("passing: " + ", ".join(b.coordinate for b in report.passing))
    print("failing: " + ", ".join(b.coordinate for b, _ in report.failing))
    return 0


# Buildscript


def wrapper_version(project: Path) -> str:
    path = project / "gradle" / "wrapper" / "gradle-wrapper.properties"
    match = re.search(r"distributionUrl=.*gradle-([0-9.]+)-", path.read_text("utf-8")) if path.is_file() else None
    return match.group(1) if match else ""


def buildscript_version(project: Path) -> str:
    """The settings plugin version, from settings.gradle.kts or an older Groovy settings.gradle."""
    for name in ("settings.gradle.kts", "settings.gradle"):
        path = project / name
        if path.is_file():
            match = re.search(r"""gtnhsettingsconvention["']\)?\s*version\s*\(?["']([^"']+)["']""",
                              path.read_text("utf-8"))
            if match:
                return match.group(1)
    return "the latest version"


def revert(project: Path) -> None:
    """Drops the working tree changes, including files a buildscript upgrade added."""
    git(project, "checkout", "--", ".")
    git(project, "clean", "-fdq")


def cmd_buildscript(args: argparse.Namespace) -> int:
    project = Path.cwd()
    github = GitHub(project, args.dry_run)
    base = git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
    before = buildscript_version(project)
    before_wrapper = wrapper_version(project)
    code, output = run(["./gradlew", "updateBuildScript"], project)
    if code != 0:
        raise RuntimeError("updateBuildScript failed")
    if "upgradeBuildScriptMajor" in output:
        # A new major buildscript; updateBuildScript only stays within the current one
        revert(project)
        code, output = run(["./gradlew", "upgradeBuildScriptMajor"], project)
        if code != 0:
            raise RuntimeError("upgradeBuildScriptMajor failed")
        # The major upgrade lands on that major's first version; this takes it to the latest one
        code, output = run(["./gradlew", "updateBuildScript"], project)
        if code != 0:
            raise RuntimeError("updateBuildScript after the major upgrade failed")
    changed = [line[3:] for line in git(project, "status", "--porcelain").splitlines()
               if line.strip() and not line[3:].startswith((".mod-workflows", ".modded-tools"))]
    github.ensure_label(BUILDSCRIPT_LABEL, "5319e7")
    # Rewritten comments or whitespace alone aren't an update
    if changed and buildscript_version(project) == before and wrapper_version(project) == before_wrapper:
        revert(project)
        changed = []
    if not changed:
        github.close_pr(BUILDSCRIPT_BRANCH, "The buildscript is up to date.")
        github.close_issues(BUILDSCRIPT_LABEL, [], "The buildscript is up to date.")
        print("buildscript up to date (" + before + ")")
        return 0
    after = buildscript_version(project)
    result = checks(project, args.server_timeout)
    title = "Bump the buildscript to " + after
    if result.ok:
        push_branch(project, BUILDSCRIPT_BRANCH, base, title, changed, args.dry_run)
        github.upsert_pr(BUILDSCRIPT_BRANCH, base, title,
                         "From " + before + " to " + after + ". The mod assembled, built with its tests and started "
                         "its dev server with it: " + run_url() + ".", BUILDSCRIPT_LABEL)
        github.close_issues(BUILDSCRIPT_LABEL, [], "The buildscript update passes now.")
    else:
        revert(project)
        github.close_pr(BUILDSCRIPT_BRANCH, "The buildscript update breaks the build now.")
        base = checks(project, args.server_timeout)
        if not base.ok:
            github.upsert_issue(BUILDSCRIPT_LABEL, "baseline", baseline_title(base), baseline_body(base))
            return 0
        github.upsert_issue(BUILDSCRIPT_LABEL, "buildscript",
                            "Updating the buildscript to " + after + " breaks the " + result.stage,
                            "Updating the buildscript from " + before + " to " + after + " fails the " + result.stage
                            + " check: " + run_url() + ".\n\n```\n" + result.tail() + "\n```")
    return 0


# README sync


def gradle_properties(project: Path) -> Dict[str, str]:
    values = {}
    path = project / "gradle.properties"
    if path.is_file():
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().replace("\\:", ":")
    return values


def request(method: str, url: str, body: dict, headers: Dict[str, str]) -> tuple:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def cmd_readme(args: argparse.Namespace) -> int:
    project = Path.cwd()
    github = GitHub(project, args.dry_run)
    properties = gradle_properties(project)
    repo = os.environ.get("GITHUB_REPOSITORY") or args.repo
    branch = args.branch or git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
    transform = [sys.executable, str(Path(args.tools) / "bin" / "transform_readme.py"), "README.md", "--branch",
                 branch] + (["--repo", repo] if repo else [])
    markdown_body = subprocess.run(transform, cwd=project, check=True, capture_output=True, text=True).stdout

    targets = [
        ("Modrinth", "modrinthProjectId", "MODRINTH_TOKEN"),
        ("67minecraft", "67minecraftProjectId", "SIX_SEVEN_TOKEN"),
        ("CurseForge", "curseForgeProjectId", "CURSEFORGE_SESSION"),
    ]
    failed = []
    for name, id_key, secret in targets:
        project_id = properties.get(id_key, "")
        token = os.environ.get(secret, "")
        if not project_id:
            print(name + ": no " + id_key + " in gradle.properties, skipped")
            continue
        if not token:
            print(name + ": no " + secret + " secret, skipped")
            continue
        if args.dry_run:
            print("[dry run] would update " + name + " project " + project_id)
            continue
        if name == "CurseForge":
            # The authors site's own request; description type 5 is Markdown, so CurseForge renders the README
            # like GitHub and Modrinth do, inline HTML comments included
            cookie = token[len("Cookie:"):].strip() if token.lower().startswith("cookie:") else token
            status, text = request("PUT", "https://authors.curseforge.com/_api/projects/description/" + project_id,
                                   {"description": markdown_body, "descriptionType": 5},
                                   {"Cookie": cookie, "User-Agent": BROWSER_USER_AGENT,
                                    "Origin": "https://authors.curseforge.com",
                                    "Referer": "https://authors.curseforge.com/", "Sec-Fetch-Dest": "empty",
                                    "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"})
            if status in (401, 403):
                github.ensure_label(README_LABEL, "c5def5")
                github.upsert_issue(README_LABEL, "curseforge-session", "The CurseForge session for README sync expired",
                                    "CurseForge refused the README update (HTTP " + str(status) + "): " + run_url()
                                    + ".\n\nLog in to authors.curseforge.com, copy the request's Cookie header and "
                                    "store it as the `CURSEFORGE_SESSION` secret again.")
            elif 200 <= status < 300:
                github.close_issues(README_LABEL, [], "The CurseForge session works again.")
        else:
            base = "https://api.modrinth.com/v2" if name == "Modrinth" else "https://67minecraft.com/api/v2"
            status, text = request("PATCH", base + "/project/" + project_id, {"body": markdown_body},
                                   {"Authorization": token})
        if 200 <= status < 300:
            print(name + ": updated project " + project_id)
        else:
            print(name + ": HTTP " + str(status) + ": " + text[:500])
            failed.append(name)
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print the GitHub and platform changes instead")
    parser.add_argument("--tools", default=".modded-tools", help="checkout of AngelicIndustries/modded_tools")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("deps", "buildscript"):
        command = sub.add_parser(name)
        command.add_argument("--server-timeout", type=int, default=300, help="seconds the dev server may take")
    readme = sub.add_parser("readme")
    readme.add_argument("--repo", help="owner/repo, when GITHUB_REPOSITORY isn't set")
    readme.add_argument("--branch", help="branch the README images link to (default: the checked out one)")
    args = parser.parse_args(argv)
    return {"deps": cmd_deps, "buildscript": cmd_buildscript, "readme": cmd_readme}[args.command](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
