"""Fix classification measured on 50 real-world commit subjects (Phase 31-3).

Each row is a subject the pre-31-3 keyword classifier tagged "fix", with a
hand label: True when the commit repairs behaviour that was wrong, False when
it is lint, formatting, a secrets baseline, a type annotation, a comment or
docs correction, a policy change or a new feature that merely says "fix".

The sample was drawn mechanically from six of the maintainer's repositories
(named ``repo-a`` to ``repo-f`` here). Population: non-merge commits touching
``*.py``, reachable from SHAs pinned at sampling time, authored since
2026-04-20, whose subject the old classifier tagged "fix". Per repo, ordered
by full commit hash; taken round-robin over the six repos until 50.

The shipped rule (a declared conventional type or gitmoji decides; the fix
keywords count only without one) misclassifies 13 of the 50, all as false
fixes. ``KNOWN_MISSES`` holds them, so a better rule shows up here as a miss
that went away, and a regression as a new one. DECISIONS.md, "Fix
classification", has the scoring of every candidate rule.
"""

import pytest

from pysmelly.git_history import classify_commit

# (repo, commit, subject, is a defect fix, why)
SAMPLE: list[tuple[str, str, str, bool, str]] = [
    (
        "repo-a",
        "0258fad5",
        "fix(mime): NEF comment points at CONTAINER_REFINEMENTS",
        False,
        "comment points at the right table",
    ),
    (
        "repo-b",
        "02ab8c46",
        "fix(lint): make check has been red since the weird tier landed",
        False,
        "lint gate repair: ruff errors in example scripts",
    ),
    (
        "repo-c",
        "033c40e0",
        'canary: fix DeleteObjectVersion probe — valid "null" id, artifact closed',
        True,
        "canary probe could not tell denied from invalid",
    ),
    (
        "repo-d",
        "36d33d59",
        "fix: deployer init wrote an [audit] key the parser discards",
        True,
        "init wrote a config key the parser discards",
    ),
    (
        "repo-e",
        "02d4b11b",
        "Clear the secrets gate: pragma the 56d fixture, correct stale baseline lines",
        False,
        "secrets gate: pragma and baseline lines",
    ),
    (
        "repo-f",
        "0a707666",
        "security: repair detect-secrets baseline and align bandit nosec",
        False,
        "detect-secrets baseline repair",
    ),
    (
        "repo-a",
        "07594578",
        "fix(oaipmh): harvest endpoints use ports 80 and 443 only (70-3)",
        False,
        "port policy change by owner decision",
    ),
    (
        "repo-b",
        "03562c27",
        "fix(alto): the Page is a coordinate system, not a claim about the image",
        True,
        "ALTO check refused every real sidecar",
    ),
    (
        "repo-c",
        "057c3740",
        "fix(contentdm): a page 404 no longer condemns the whole compound",
        True,
        "one page 404 condemned a whole compound",
    ),
    (
        "repo-d",
        "39751162",
        "fix(service): a load-balanced service with no target group is an error (tracking item 69, member 3)",
        True,
        "service was created silently unreachable",
    ),
    (
        "repo-e",
        "0faa92a4",
        "review(2026-09-18): PYTHON - gate Python formatting, and fix the file that proves it was ungated",
        False,
        "formatting gate added, one file reformatted",
    ),
    (
        "repo-f",
        "1026756f",
        "SECURITY + DOCKER: icons mark_safe fix, uv image pin 0.5->0.12, doc staleness",
        False,
        "bandit finding re-fixed, image pin, doc staleness",
    ),
    (
        "repo-a",
        "1106f6f5",
        "fix(crosswalks): make split/join transforms actually split and join",
        True,
        "split/join transforms did not split or join",
    ),
    (
        "repo-b",
        "03af8ea4",
        "fix(review): key the verdict store by pool, and filter what it hands back",
        True,
        "verdict store leaked a foreign pool's verdicts into downloads",
    ),
    (
        "repo-c",
        "0d6855b3",
        "fix(aws_backup): ConfigError points at the setup doc that exists",
        True,
        "error messages named a doc that does not exist",
    ),
    (
        "repo-d",
        "46b28b5b",
        "PYTHON/MAKEFILE review: add ruff UP rules, fix install target",
        True,
        "make install stripped the dev group and broke every target",
    ),
    (
        "repo-e",
        "1719b72c",
        "Fix bigm index to actually serve the icontains query path",
        True,
        "search index was never used by the query",
    ),
    (
        "repo-f",
        "1ee259a8",
        "restore: fix leaking multi-line {# #} comments + reword clamp flashes",
        True,
        "template comments leaked into rendered HTML",
    ),
    (
        "repo-a",
        "128ff835",
        "fix(iiif): refuse an empty access-check secret; one anchored dev value",
        True,
        "empty secret let unauthenticated requests pass",
    ),
    (
        "repo-b",
        "094e695a",
        "docs(55-1): fix round 3, the three held counts re-derived from the rows",
        False,
        "documentation counts corrected",
    ),
    (
        "repo-c",
        "108a06e8",
        "chore: fix pre-existing ruff lint errors",
        False,
        "ruff lint errors",
    ),
    (
        "repo-d",
        "651378a4",
        "fix(deploy): every Phase 69 check runs before anything moves (tracking item 69, validator N1)",
        True,
        "deploy moved services before every check ran",
    ),
    (
        "repo-e",
        "1e2481bc",
        "PYSMELLY-REVIEW: scope fix, FP suppression, re-evaluate tags",
        False,
        "pysmelly review: scope and suppression tags",
    ),
    (
        "repo-f",
        "4fea4ecd",
        "Fix: rename handles Globus directory-placeholder objects (no ContentLength)",
        True,
        "rename failed on directory-placeholder objects",
    ),
    (
        "repo-a",
        "14096d84",
        "fix(works): four defects found by the bug-corpus IIIF slice",
        True,
        "four defects from the bug corpus",
    ),
    (
        "repo-b",
        "0a2460e5",
        "fix(samples): 46-1's agreement check runs last and untimed",
        True,
        "quadratic comparison stalled the sample run for half an hour",
    ),
    (
        "repo-c",
        "19030399",
        "fix(scratch-host): lend the ECR token to the ssh user, not root",
        True,
        "ECR token went to root, where no pull looks",
    ),
    (
        "repo-d",
        "6521c9cc",
        "fix(env): an unresolved ${...} in an environment value is an error (tracking item 69, members 1+7)",
        True,
        "unresolved placeholder passed through silently",
    ),
    (
        "repo-e",
        "56479840",
        'Fix delimited_vals rendering literal "None" for empty values',
        True,
        "empty values rendered as literal None",
    ),
    (
        "repo-f",
        "5a0bad4b",
        "restore UI: fix folder-name template bug + skip empty-key directory markers",
        True,
        "folder-name template bug",
    ),
    (
        "repo-a",
        "158a4dee",
        "fix(sword): TEI header extraction saves only the fields it fills (144-3)",
        True,
        "full save overwrote a concurrent access-level change",
    ),
    (
        "repo-b",
        "0b8a6fda",
        "fix(62-2 row 5): the scene kit's checksum pragmas take their own lines",
        False,
        "lint: pragma moved to its own line",
    ),
    (
        "repo-c",
        "1be6967c",
        "fix(contentdm): re-ask partial compounds' missing pages, retire them after 3 runs",
        True,
        "a page lost to one transient 404 stayed lost",
    ),
    (
        "repo-d",
        "6621e139",
        "fix(service): --dry-run previews create or update truthfully, with its parameters (tracking item 69, member 6)",
        True,
        "--dry-run preview was untruthful",
    ),
    (
        "repo-e",
        "d739defc",
        "Fix date column rendering: normalize ingest, tolerate legacy datetimes",
        True,
        "date column rendering broke on legacy datetimes",
    ),
    (
        "repo-f",
        "686c61be",
        "Fix: chained-resume pre-flight + LIST fallback for missing ContentLength",
        True,
        "chained resume flagged its own moved keys as obstacles",
    ),
    (
        "repo-a",
        "195c5a8d",
        "Fix stale-job sweeper race, heartbeat keys, and staleness signal",
        True,
        "sweeper race",
    ),
    (
        "repo-b",
        "0c8541ff",
        "fix(80-1): box grouping keyed by creation sequence, not id()",
        True,
        "grouping keyed by id(), which is reused",
    ),
    (
        "repo-c",
        "1d1a6c90",
        "nightly-job: audit + audit-fix for archive parse failures",
        False,
        "new audit job (audit-fix is a make target)",
    ),
    (
        "repo-d",
        "7fc36b6b",
        "fix(placeholders): bools are never spelled Python-style (tracking item 69, member 8)",
        True,
        "bools rendered Python-style in output",
    ),
    (
        "repo-e",
        "dc2c0c3d",
        "Fix FTS foundation: Postgres tests, canonical rebuild writer, bigm index",
        True,
        "search trigger and index lost; production search unindexed",
    ),
    (
        "repo-f",
        "7a32ed05",
        "Fix: htmx folder navigation now updates breadcrumbs + external links",
        True,
        "htmx navigation left breadcrumbs stale",
    ),
    (
        "repo-a",
        "1e3b413b",
        "fix(transcoder): type the failure payloads the transport writes",
        False,
        "type annotation for pyright",
    ),
    (
        "repo-b",
        "0dff9b0a",
        "fix(bench): the cold phase was measuring the benchmark's own footprint",
        True,
        "benchmark reported the wrong timing",
    ),
    (
        "repo-c",
        "26ff508e",
        "fix(contentdm): re-ask gone pointers, with three brakes on the cost",
        True,
        "one transient 404 condemned an item forever",
    ),
    (
        "repo-d",
        "83c88908",
        "FIX: ssm-secrets.py check crashed on the documented [secrets] names form",
        True,
        "check crashed on a documented config form",
    ),
    (
        "repo-e",
        "dc2edca3",
        "ASYNC-JOBS + TOFU first audits: beat runs locally, tz fix, tofu fmt",
        True,
        "Celery ran on the wrong timezone",
    ),
    (
        "repo-f",
        "8d126019",
        "S7a: env-fallbacks batch 2 — required creds + hardcoded rates + chain fix (12)",
        False,
        "env-fallback removal, a design cleanup",
    ),
    (
        "repo-a",
        "221356d7",
        "docs: owner accepts 149-4; correct the get_exclude docstring",
        False,
        "docstring correction",
    ),
    (
        "repo-b",
        "0f40a1c1",
        "fix(62-4): allowlist the checkpoint-class revision pins",
        False,
        "detect-secrets allowlist pragmas",
    ),
]

# Subjects the shipped rule gets wrong: each declares a fix (a ``fix(...)``
# prefix) or says "fix" under a non-conventional prefix, and is not one.
KNOWN_MISSES = frozenset(
    {
        "0258fad5",
        "02ab8c46",
        "02d4b11b",
        "0a707666",
        "07594578",
        "0faa92a4",
        "1026756f",
        "1e2481bc",
        "0b8a6fda",
        "1d1a6c90",
        "1e3b413b",
        "8d126019",
        "0f40a1c1",
    }
)


def test_sample_is_fifty_subjects_with_unique_commits():
    commits = {commit for _, commit, _, _, _ in SAMPLE}
    assert len(SAMPLE) == 50
    assert len(commits) == 50
    assert KNOWN_MISSES.issubset(commits)


@pytest.mark.parametrize(
    ("subject", "is_fix", "commit"),
    [(subject, is_fix, commit) for _, commit, subject, is_fix, _ in SAMPLE],
    ids=[f"{repo}-{commit}" for repo, commit, _, _, _ in SAMPLE],
)
def test_sampled_subject_verdict(subject: str, is_fix: bool, commit: str):
    classified_fix = "fix" in classify_commit(subject)
    if commit in KNOWN_MISSES:
        assert classified_fix is not is_fix, f"{commit} is no longer a miss: drop it"
    else:
        assert classified_fix is is_fix
