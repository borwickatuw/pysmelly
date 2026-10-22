"""Output formatting for findings."""

import random
from collections import Counter

from pysmelly.chains import Chain
from pysmelly.registry import Finding, Severity

_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}

_TAGLINES = [
    ("Whoever smelt it, committed it.", None),
    ("I love the smell of refactoring in the morning.", "Apocalypse Now"),
    ("I smell dead code.", "The Sixth Sense"),
    ("Something is rotten in the state of your codebase.", "Hamlet"),
    ("Houston, we have a code smell.", "Apollo 13"),
    ("You can't handle the smell!", "A Few Good Men"),
    ("This code doesn't pass the smell test.", None),
    ("Follow your nose -- it always knows the code that smells.", "Toucan Sam"),
    ("Here's looking at you, cruft.", "Casablanca"),
    ("What's that smell? Oh, it's technical debt.", None),
    ("May the refactor be with you.", "Star Wars"),
    ("After all, tomorrow is another sprint.", "Gone with the Wind"),
    ("We're gonna need a bigger backlog.", "Jaws"),
    (
        "To refactor, or not to refactor -- that is not the question. Refactor.",
        "Hamlet",
    ),
    ("Elementary, my dear developer. It's dead code.", "Sherlock Holmes"),
    ("There's no place like a clean codebase.", "The Wizard of Oz"),
    ("One does not simply ignore code smells.", "The Lord of the Rings"),
    ("I'm gonna make you a refactor you can't refuse.", "The Godfather"),
    ("It's alive! ... unfortunately.", "Frankenstein"),
    ("In the room, the engineers come and go, talking of TODO.", "T.S. Eliot"),
    ("The first rule of dead code: you do not commit dead code.", "Fight Club"),
    (
        "All those abstractions will be lost in time, like tears in rain.",
        "Blade Runner",
    ),
    ("Toto, I've a feeling we're not in clean code anymore.", "The Wizard of Oz"),
    ("You shall not deploy!", "The Lord of the Rings"),
    ("I find your lack of tests disturbing.", "Star Wars"),
    ("Say hello to my little linter.", "Scarface"),
    (
        "Life is like a box of legacy code. You never know what you're gonna get.",
        "Forrest Gump",
    ),
    ("We have nothing to fear but spaghetti code itself.", "FDR"),
    ("Go ahead, make my pull request.", "Dirty Harry"),
    ("I think, therefore I refactor.", "Descartes"),
    ("Et tu, junior dev?", "Julius Caesar"),
    ("It was the best of code, it was the worst of code.", "A Tale of Two Cities"),
    (
        "That's one small step for a dev, one giant leap for code quality.",
        "Neil Armstrong",
    ),
    ("Ask not what your codebase can do for you.", "JFK"),
    ("Frankly, my dear, I don't give a diff.", "Gone with the Wind"),
    ("We choose to refactor not because it is easy, but because it is hard.", "JFK"),
    ("The only thing worse than dead code is dead code that almost works.", None),
    ("Hasta la vista, dead code.", "The Terminator"),
    ("I'll be back... to refactor this.", "The Terminator"),
    ("Keep your friends close, and your abstractions closer.", "The Godfather Part II"),
    ("Nobody puts dead code in production.", "Dirty Dancing"),
    ("You had me at 'refactor'.", "Jerry Maguire"),
    ("What we've got here is failure to decompose.", "Cool Hand Luke"),
    ("Mrs. Robinson, you're trying to commit dead code.", "The Graduate"),
    ("Roads? Where we're going, we don't need dead code.", "Back to the Future"),
    ("Abandon all defaults, ye who enter here.", "Dante"),
    ("The unexamined codebase is not worth deploying.", "Socrates"),
    (
        "Reports of this function's usefulness have been greatly exaggerated.",
        "Mark Twain",
    ),
    ("Smells like dead code spirit.", "Nirvana"),
    (
        "In this world, nothing is certain except dead code and taxes.",
        "Benjamin Franklin",
    ),
]


def _stable_order(findings: list[Finding]) -> list[Finding]:
    """The one canonical order for a set of findings.

    Checks assemble their findings by walking dicts and sets keyed by file
    paths and names, and Python randomizes string hashing per interpreter run,
    so the same tree could print the same findings in a different order on the
    next run. That makes a before/after diff of a remediation unreadable --
    the method PYSMELLY-REVIEW is built on. Sorting once at the output boundary
    fixes it for every check, including checks not written yet.

    The key is total: severity (highest first), then check, file, line and
    message. Two findings that tie on all five are indistinguishable in the
    output, so their relative order cannot be observed. Section order follows
    from it, because sections are grouped in first-appearance order.
    """
    return sorted(
        findings,
        key=lambda f: (_SEVERITY_ORDER[f.severity], f.check, f.file, f.line, f.message),
    )


def _rank_findings(findings: list[Finding]) -> list[Finding]:
    """Sort findings by confidence: severity desc, then check hit-count asc.

    Within a severity tier, findings from checks with fewer hits are ranked
    higher — a check that fired twice is higher signal than one that fired 40 times.
    """
    check_counts: dict[str, int] = {}
    for f in findings:
        check_counts[f.check] = check_counts.get(f.check, 0) + 1

    return sorted(
        findings,
        key=lambda f: (_SEVERITY_ORDER[f.severity], check_counts[f.check]),
    )


def _capped(findings: list[Finding], max_findings: int) -> list[Finding]:
    """The findings the display keeps: the ``max_findings`` highest-confidence, or all.

    ``findings`` is in canonical order (``_stable_order``); every finding is
    kept when ``max_findings`` is 0 or not exceeded.
    """
    if max_findings > 0 and len(findings) > max_findings:
        return _rank_findings(findings)[:max_findings]
    return findings


def printed_findings(findings: list[Finding], *, summary: bool, max_findings: int) -> list[Finding]:
    """The findings ``format_text`` will print one by one, given the same arguments.

    None under ``summary``, which prints counts; otherwise the ones the cap
    keeps, and every member of a chain one of those belongs to, which the
    chain section lists (``_shown_chains``). Work done per printed finding
    (a provenance line) is done for these and nothing else.
    """
    if summary:
        return []
    ordered = _stable_order(findings)
    capped = _capped(ordered, max_findings)
    kept = {id(f) for f in capped}
    chained = (f for members in _shown_chains(ordered, capped).values() for f in members)
    return [*capped, *(f for f in chained if id(f) not in kept)]


def _shown_chains(
    findings: list[Finding], display_findings: list[Finding]
) -> dict[Chain, list[Finding]]:
    """Each chain a displayed finding belongs to, with every one of its members.

    A chain is one work item (``chains.py``), so it lists the members the
    display cap left out too. Chains go in the order their first displayed
    member was ranked; members by file, line, check and message.
    """
    members: dict[Chain, list[Finding]] = {}
    for f in display_findings:
        if f.chain is not None:
            members.setdefault(f.chain, [])
    for f in findings:
        if f.chain in members:
            members[f.chain].append(f)
    return {
        chain: sorted(chained, key=lambda f: (f.file, f.line, f.check, f.message))
        for chain, chained in members.items()
    }


def _format_chains(findings: list[Finding], display_findings: list[Finding]) -> list[str]:
    """The finding chains section: each chain a displayed finding belongs to, once.

    Every member prints under the chain's title by file and line, with its
    ``notes`` under it, and not again under its check.
    """
    members = _shown_chains(findings, display_findings)
    if not members:
        return []
    count = sum(len(chained) for chained in members.values())
    lines = [f"=== finding chains ({len(members)} work item(s), {count} finding(s)) ==="]
    for chain, chained in members.items():
        lines.append(f"  {chain.title}")
        for f in chained:
            lines.append(f"    {f.file}:{f.line}: [{f.check}] {f.text}")
            lines.extend(f"      {note}" for note in f.notes)
    lines.append("")
    return lines


def _format_hotspots(findings: list[Finding], max_findings: int) -> list[str]:
    """The convergence hotspots section: files three or more checks flag.

    Printed first, as the highest-confidence signal. Every finding counts,
    shown or not, so a file is a hotspot whatever the cap displays. Files
    rank by how many checks flag them, then by path; when ``max_findings``
    is set the section lists that many and its heading says so.
    """
    file_checks: dict[str, set[str]] = {}
    for f in findings:
        file_checks.setdefault(f.file, set()).add(f.check)
    hotspots = sorted(
        ((path, checks) for path, checks in file_checks.items() if len(checks) >= 3),
        key=lambda item: (-len(item[1]), item[0]),
    )
    if not hotspots:
        return []
    shown = hotspots[:max_findings] if max_findings > 0 else hotspots
    cut = "" if len(shown) == len(hotspots) else f", {len(shown)} shown"
    lines = [f"=== convergence hotspots ({len(hotspots)} file(s){cut}) ==="]
    for path, checks in shown:
        lines.append(f"  {path}: flagged by {len(checks)} checks ({', '.join(sorted(checks))})")
    lines.append("")
    return lines


def _format_check_sections(findings: list[Finding], display_findings: list[Finding]) -> list[str]:
    """One section per check with a displayed unchained finding, its findings under it.

    The heading counts the check's unchained findings, shown or not, so a
    capped view never under-reports a check, and says how many the cap
    shows when it cut the section short. Chain members print in the chain.
    """
    unchained_counts = Counter(f.check for f in findings if f.chain is None)
    by_check: dict[str, list[Finding]] = {}
    for f in (f for f in display_findings if f.chain is None):
        by_check.setdefault(f.check, []).append(f)
    lines = []
    for check_name, check_findings in by_check.items():
        total, shown = unchained_counts[check_name], len(check_findings)
        cut = "" if shown == total else f", {shown} shown"
        lines.append(f"=== {check_name} ({total} finding(s){cut}) ===")
        for f in check_findings:
            lines.append(f"  {f.file}:{f.line}: {f.text}")
            lines.extend(f"    {note}" for note in f.notes)
        lines.append("")
    return lines


def format_guidance(context: list[str] | None) -> list[str]:
    """The guidance preamble's lines, or none when there is no guidance to give."""
    if not context:
        return []
    return ["--- Guidance ---", *context, "----------------", ""]


def format_text(
    findings: list[Finding],
    total_files: int,
    context: list[str] | None,
    *,
    summary: bool,
    max_findings: int,
    header: list[str],
) -> str:
    """Text output grouped by check, with optional guidance preamble.

    max_findings: if > 0, show only the top N highest-confidence findings.
    header: lines under the file count naming what the run looked at — the
    git-history window and run date, so a quoted count carries it, and the
    informational ``Project trends`` block. They are not findings and never
    touch the counts.

    A displayed finding's ``notes`` print under it, indented.

    Findings are put in canonical order first, so two runs over the same tree
    produce byte-identical output apart from the closing tagline.
    """
    findings = _stable_order(findings)
    lines = format_guidance(context)

    lines.extend([f"Parsed {total_files} Python files", *header, ""])

    total_count = len(findings)
    display_findings = _capped(findings, max_findings)
    # A summary prints every check's count, so nothing is left to show.
    suppressed_count = 0 if summary else total_count - len(display_findings)

    lines.extend(_format_hotspots(findings, max_findings))
    if not summary:
        lines.extend(_format_chains(findings, display_findings))

    if summary:
        # Summary mode uses all findings for counts, not truncated
        all_by_check: dict[str, list[Finding]] = {}
        for f in findings:
            all_by_check.setdefault(f.check, []).append(f)
        sorted_checks = sorted(
            all_by_check.items(),
            key=lambda item: (_SEVERITY_ORDER[item[1][0].severity], -len(item[1]), item[0]),
        )
        for check_name, check_findings in sorted_checks:
            severity = check_findings[0].severity.value
            lines.append(f"  {check_name:<30} [{severity:<6}]  {len(check_findings)}")
        lines.append("")
    else:
        lines.extend(_format_check_sections(findings, display_findings))

    if findings:
        lines.append(f"Total: {total_count} finding(s)")
        if suppressed_count > 0:
            lines.append(
                f"Showing top {max_findings}. Run with --more-please for all {total_count}."
            )
    else:
        lines.append("All checks passed.")

    if findings:
        quote, source = random.choice(_TAGLINES)
        attribution = f" ({source})" if source else ""
        lines.append(f"\n  -- {quote}{attribution}")

    return "\n".join(lines)
