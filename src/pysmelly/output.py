"""Output formatting for findings."""

import random

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
    ("To refactor, or not to refactor -- that is not the question. Refactor.", "Hamlet"),
    ("Elementary, my dear developer. It's dead code.", "Sherlock Holmes"),
    ("There's no place like a clean codebase.", "The Wizard of Oz"),
    ("One does not simply ignore code smells.", "The Lord of the Rings"),
    ("I'm gonna make you a refactor you can't refuse.", "The Godfather"),
    ("It's alive! ... unfortunately.", "Frankenstein"),
    ("In the room, the engineers come and go, talking of TODO.", "T.S. Eliot"),
    ("The first rule of dead code: you do not commit dead code.", "Fight Club"),
    ("All those abstractions will be lost in time, like tears in rain.", "Blade Runner"),
    ("Toto, I've a feeling we're not in clean code anymore.", "The Wizard of Oz"),
    ("You shall not deploy!", "The Lord of the Rings"),
    ("I find your lack of tests disturbing.", "Star Wars"),
    ("Say hello to my little linter.", "Scarface"),
    ("Life is like a box of legacy code. You never know what you're gonna get.", "Forrest Gump"),
    ("We have nothing to fear but spaghetti code itself.", "FDR"),
    ("Go ahead, make my pull request.", "Dirty Harry"),
    ("I think, therefore I refactor.", "Descartes"),
    ("Et tu, junior dev?", "Julius Caesar"),
    ("It was the best of code, it was the worst of code.", "A Tale of Two Cities"),
    ("That's one small step for a dev, one giant leap for code quality.", "Neil Armstrong"),
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
    ("Reports of this function's usefulness have been greatly exaggerated.", "Mark Twain"),
    ("Smells like dead code spirit.", "Nirvana"),
]


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


def format_text(
    findings: list[Finding],
    total_files: int,
    context: list[str] | None,
    summary: bool = False,
    max_findings: int = 0,
) -> str:
    """Text output grouped by check, with optional guidance preamble.

    max_findings: if > 0, show only the top N highest-confidence findings.
    """
    lines = []

    if context:
        lines.append("--- Guidance ---")
        for item in context:
            lines.append(item)
        lines.append("----------------")
        lines.append("")

    lines.extend([f"Parsed {total_files} Python files", ""])

    # Determine which findings to display
    total_count = len(findings)
    if max_findings > 0 and total_count > max_findings:
        display_findings = _rank_findings(findings)[:max_findings]
        suppressed_count = total_count - max_findings
    else:
        display_findings = findings
        suppressed_count = 0

    by_check: dict[str, list[Finding]] = {}
    for f in display_findings:
        by_check.setdefault(f.check, []).append(f)

    if summary:
        # Summary mode uses all findings for counts, not truncated
        all_by_check: dict[str, list[Finding]] = {}
        for f in findings:
            all_by_check.setdefault(f.check, []).append(f)
        sorted_checks = sorted(
            all_by_check.items(),
            key=lambda item: (_SEVERITY_ORDER[item[1][0].severity], -len(item[1])),
        )
        for check_name, check_findings in sorted_checks:
            severity = check_findings[0].severity.value
            lines.append(f"  {check_name:<30} [{severity:<6}]  {len(check_findings)}")
        lines.append("")
    else:
        for check_name, check_findings in by_check.items():
            lines.append(f"=== {check_name} ({len(check_findings)} finding(s)) ===")
            for f in check_findings:
                lines.append(f"  {f.file}:{f.line}: {f.message}")
            lines.append("")

    if findings:
        lines.append(f"Total: {total_count} finding(s)")
        if suppressed_count > 0:
            lines.append(
                f"Showing top {max_findings}." f" Run with --more-please for all {total_count}."
            )
    else:
        lines.append("All checks passed.")

    if findings:
        quote, source = random.choice(_TAGLINES)
        attribution = f" ({source})" if source else ""
        lines.append(f"\n  -- {quote}{attribution}")

    return "\n".join(lines)
