"""Git history checks — detect evolutionary signals invisible to static analysis."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from pysmelly.context import AnalysisContext
from pysmelly.registry import Finding, Severity, check

# Files that are naturally stable and shouldn't be flagged
_SKIP_NAMES = frozenset({"__init__.py", "conftest.py"})

# Files that match these patterns are config-like and naturally stable
_SKIP_SUFFIXES = ("_config.py", "_settings.py", "settings.py", "config.py")

_MONTHS_STALE = 12
_MONTHS_ACTIVE = 6


@check(
    "abandoned-code",
    severity=Severity.LOW,
    category="git-history",
    description="Files untouched 12+ months while directory peers keep evolving",
)
def check_abandoned_code(ctx: AnalysisContext) -> list[Finding]:
    history = ctx.git_history
    if history is None:
        return []

    now = datetime.now(timezone.utc)
    findings: list[Finding] = []

    # Group files by parent directory
    dir_files: dict[str, list[str]] = {}
    for file_path in ctx.all_trees:
        file_str = str(file_path)
        parent = str(file_path.parent)
        dir_files.setdefault(parent, []).append(file_str)

    for dir_name, files in dir_files.items():
        # Need >= 3 files to establish a meaningful peer group
        if len(files) < 3:
            continue

        # Collect last-modified dates for files that have git history
        file_dates: dict[str, datetime] = {}
        for f in files:
            last_mod = history.last_modified.get(f)
            if last_mod is not None:
                file_dates[f] = last_mod

        if not file_dates:
            continue

        # Compute median last-modified for the directory
        dates = list(file_dates.values())
        median_timestamp = median(d.timestamp() for d in dates)
        median_date = datetime.fromtimestamp(median_timestamp, tz=timezone.utc)

        # If the directory median is old (peers aren't active), skip
        months_since_median = (now - median_date).days / 30.44
        if months_since_median >= _MONTHS_ACTIVE:
            continue

        # Find stale files in active directories
        for f in files:
            name = Path(f).name
            if name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES):
                continue

            last_mod = file_dates.get(f)
            if last_mod is None:
                # File not in git history (new/untracked) — skip
                continue

            months_stale = (now - last_mod).days / 30.44
            if months_stale < _MONTHS_STALE:
                continue

            # Count active peers
            active_peers = sum(
                1
                for peer_date in file_dates.values()
                if (now - peer_date).days / 30.44 < _MONTHS_ACTIVE
            )
            total_peers = len(files)

            date_str = last_mod.strftime("%Y-%m-%d")
            months_ago = int(months_stale)
            findings.append(
                Finding(
                    file=f,
                    line=1,
                    check="abandoned-code",
                    message=(
                        f"{f} last modified {date_str} ({months_ago} months ago), "
                        f"but {active_peers} of {total_peers} peers in {dir_name}/ "
                        f"changed in last {_MONTHS_ACTIVE} months"
                    ),
                    severity=Severity.LOW,
                )
            )

    return findings
