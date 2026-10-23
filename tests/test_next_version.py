"""scripts/next-version.sh decides every release number, so pin its bump rules."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "next-version.sh"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                        "PATH": "/usr/bin:/bin:/usr/local/bin"})


def _commit(repo, message):
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)


def _next(repo):
    out = subprocess.run(["bash", str(SCRIPT)], cwd=repo, check=True, capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.split())


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "chore: start")
    return tmp_path


@pytest.fixture
def tagged(repo):
    _git(repo, "tag", "v1.4.2")
    return repo


@pytest.mark.unit
def test_no_tags_bootstraps_v1(repo):
    assert _next(repo) == {"VERSION": "v1.0.0", "BUMP": "initial"}


@pytest.mark.unit
@pytest.mark.parametrize("message, version, bump", [
    ("feat!: drop the legacy API", "v2.0.0", "major"),
    ("refactor(db)!: new schema", "v2.0.0", "major"),
    ("feat: thing\n\nBREAKING CHANGE: config renamed", "v2.0.0", "major"),
    ("feat: add route planner", "v1.5.0", "minor"),
    ("feat(testers): add Ondilo", "v1.5.0", "minor"),
    ("fix: off-by-one", "v1.4.3", "patch"),
    ("fix(deps): bump pypdf", "v1.4.3", "patch"),
    ("perf: faster today page", "v1.4.3", "patch"),
    ("revert: undo the planner", "v1.4.3", "patch"),
    ('Revert "feat: add route planner"', "v1.4.3", "patch"),
    ("docs: readme", "v1.4.2", "none"),
    ("chore: tidy", "v1.4.2", "none"),
    ("ci: tweak", "v1.4.2", "none"),
    ("merge: branch", "v1.4.2", "none"),
])
def test_bump_rules(tagged, message, version, bump):
    _commit(tagged, message)
    assert _next(tagged) == {"VERSION": version, "BUMP": bump}


@pytest.mark.unit
def test_highest_bump_in_range_wins(tagged):
    for message in ("fix: a", "docs: b", "feat: c", "fix: d"):
        _commit(tagged, message)
    assert _next(tagged) == {"VERSION": "v1.5.0", "BUMP": "minor"}


@pytest.mark.unit
def test_only_commits_since_last_tag_count(tagged):
    _commit(tagged, "feat: shipped already")
    _git(tagged, "tag", "v1.5.0")
    _commit(tagged, "fix: new")
    assert _next(tagged) == {"VERSION": "v1.5.1", "BUMP": "patch"}
