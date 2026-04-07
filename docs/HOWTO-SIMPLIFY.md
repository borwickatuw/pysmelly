# HOWTO-SIMPLIFY: Technical Debt Tracking

This file tracks files exceeding the size thresholds defined in claude-meta `best-practices/DOCS.md`. See that guide for the simplification methodology.

**Thresholds:**
- Python scripts/modules: 300 lines
- Test files: 400 lines

## Status

Last reviewed: 2026-04-07 (Session 15 of comprehensive review)

The following files exceed thresholds. They are organized as **one file per check category**, which is the project's intentional structure (each check module groups related AST/cross-file checks). However, several modules have grown to the point where they should be considered for sub-decomposition.

## Long check functions (suppressed inline pending refactor)

These three check functions exceed pysmelly's own `long-function` threshold (100+ lines) and are currently suppressed with `# pysmelly: ignore[long-function]`. Refactoring them is part of the follow-up simplification phase.

| Function | File | Lines | Notes |
|----------|------|------:|-------|
| `check_temp_accumulators` | `src/pysmelly/checks/patterns.py:372` | 112 | Could split into helper functions for finding append targets vs analyzing usage |
| `check_fossilized_toggles` | `src/pysmelly/checks/patterns.py:1037` | 114 | Could split: find UPPER_CASE constants, find their references in conditionals, classify each |
| `check_law_of_demeter` | `src/pysmelly/checks/patterns.py:2310` | 102 | Could extract attribute-chain depth helper |

## Source files exceeding 300 lines

| File | Lines | Status | Notes |
|------|------:|--------|-------|
| `src/pysmelly/checks/patterns.py` | 2411 | **Candidate for split** | 17 pattern-based checks. Could split into `patterns_data.py` (foo-equals-foo, dict-as-dataclass, etc.), `patterns_control.py` (suspicious-fallbacks, exception-flow-control, unreachable-after-return), `patterns_naming.py` (hungarian-notation, getattr-strings), and `patterns_misc.py` (rest). |
| `src/pysmelly/checks/history.py` | 1410 | **Candidate for split** | 15 git-history checks. Natural split: `history_growth.py` (growth-trajectory, churn-without-growth, hotspot-acceleration), `history_bugs.py` (bug-magnet, fix-propagation, fix-follows-feature, stabilization-failure), `history_team.py` (knowledge-silo, abandoned-code, divergent-change), `history_coupling.py` (blast-radius, change-coupling, conscious-debt, no-refactoring, test-erosion). |
| `src/pysmelly/checks/callers.py` | 1326 | Watch | 10 cross-file call-graph checks. Currently coherent (all about call-graph). Split if it grows past ~1500. |
| `src/pysmelly/checks/structure.py` | 1052 | Watch | 11 structural checks. Currently coherent. Split if it grows past ~1500. |
| `src/pysmelly/cli.py` | 1041 | Watch | Click subcommands + suppression logic + output routing. Could extract `cli_suppression.py` (handles `# pysmelly: ignore` parsing) and `cli_init.py` (init subcommand for guidance file). |
| `src/pysmelly/checks/repetition.py` | 913 | Watch | 4 repetition checks but each is large (scattered-constants, shotgun-surgery, repeated-string-parsing). Currently fine. |
| `src/pysmelly/checks/architecture.py` | 822 | Watch | 8 architectural checks. Currently fine. |
| `src/pysmelly/git_history.py` | 602 | Acceptable | Single coherent module: git log parsing + commit classification. Above 300 but cohesive. |
| `src/pysmelly/checks/dead.py` | 472 | Acceptable | 5 dead code extension checks. Above 300 but coherent. |
| `src/pysmelly/checks/helpers.py` | 461 | Acceptable | Shared AST utilities. Above 300 but coherent. |

## Test files exceeding 400 lines

| File | Lines | Status | Notes |
|------|------:|--------|-------|
| `tests/test_patterns.py` | 2253 | **Track with patterns.py** | Mirrors patterns.py structure. If patterns.py is split, split tests with it. |
| `tests/test_history_checks.py` | 2225 | **Track with history.py** | Mirrors history.py structure. If history.py is split, split tests with it. |
| `tests/test_callers.py` | 1697 | Watch | Mirrors callers.py |
| `tests/test_structure.py` | 1069 | Watch | Mirrors structure.py |
| `tests/test_dead.py` | 857 | Watch | Mirrors dead.py |
| `tests/test_architecture.py` | 778 | Watch | Mirrors architecture.py |
| `tests/test_repetition.py` | 759 | Watch | Mirrors repetition.py |
| `tests/test_cli.py` | 570 | Watch | CLI behavior tests |
| `tests/test_git_history.py` | 449 | Watch | Just over threshold, coherent |

## Methodology Notes

The project's "one file per check category" organization is intentional and documented in `docs/DECISIONS.md`. The candidate splits above preserve this principle by creating sub-categories within the larger files (e.g., splitting `patterns.py` into `patterns_data.py`, `patterns_control.py`, etc.).

Before splitting any file:
1. Read `claude-meta/best-practices/DOCS.md` Practice 1 for the simplification methodology
2. Run tests before and after
3. Make small focused commits
4. Don't change check behavior — only file organization
5. Update `src/pysmelly/checks/__init__.py` to import the new modules (so registration still works)
6. Update `CLAUDE.md` "Key Files" section to reflect the new structure

## Follow-up phase

This file was created during Session 15 of the comprehensive review. The user requested that a follow-up phase work through these candidates. See claude-meta `docs/PLAN.md` for the scheduled work.
