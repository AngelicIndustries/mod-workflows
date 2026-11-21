# mod-workflows

Reusable GitHub workflows that keep Angelic Industries' GTNH-buildscript mods up to date.

* **Dependency updates:** weekly, bumps `dependencies.gradle` with [modded_tools](https://github.com/AngelicIndustries/modded_tools)' update checker. The bumps that pass go into one rolling PR; each bump that breaks something gets an issue with the log. A bump passes when the mod assembles, builds with its tests and Spotless check, and its dev server starts and stops cleanly, the same checks as GTNH's build-and-test. The dev server gets a free port and, when it has none, the Wawel Auth dev settings from the mod conventions. Dependencies listed in `.github/dependency-updates.ignore` are skipped, and so are LWJGL, which Minecraft pins, and Mockito, whose releases after 4.11.0 need a newer Java than the tests run on.
* **Buildscript update:** weekly, runs `updateBuildScript` (or `upgradeBuildScriptMajor` when a new major buildscript is out) and opens a PR if it passes the same checks, an issue if it doesn't.
* **README sync:** when `README.md` or `images/` change, pushes the README to the Modrinth, 67minecraft and CurseForge pages whose project IDs are set in `gradle.properties`. Relative images link the repo's raw files, which only works for public repos.

## Use

Copy the files in [callers](callers) into a mod's `.github`: `maintenance.yml` and `readme-sync.yml` into `.github/workflows`, and `dependency-updates.ignore` into `.github` if some dependencies should stay put.

* The repo, or the organization, must allow GitHub Actions to create pull requests (Settings, Actions, General, Workflow permissions).
* README sync uses the `MODRINTH_TOKEN` and `SIX_SEVEN_TOKEN` secrets the release workflow already has. CurseForge has no API for descriptions, so it needs a `CURSEFORGE_SESSION` secret: `CobaltSession=...`, the session cookie of a logged-in authors.curseforge.com session. When it expires, the sync opens an issue.
* PRs opened with the workflow's own token don't start the mod's other workflows, so the checks run in the maintenance job itself before a PR is opened.
* To have the bot appear under its own name and picture, create a GitHub App in the organization with read and write access to contents, pull requests and issues, install it on the mods, and set its ID and private key as the `MAINTENANCE_APP_ID` and `MAINTENANCE_APP_PRIVATE_KEY` secrets. Its PRs also start the mod's own CI. Without them, the bot is github-actions[bot].

The maintenance builds fetch the OvermindDL1 Forge mirror, which times out on GitHub's runners, from the Angelic Maven, which mirrors it. The [Check the org secrets](.github/workflows/check-secrets.yml) workflow tests the organization's publishing secrets.

Run the scripts locally with `scripts/maintain.py --dry-run --tools <modded_tools checkout> deps|buildscript|readme` from a mod's folder.
