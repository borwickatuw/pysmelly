# HOWTO-SIMPLIFY: Technical Debt Tracking

This file tracks files exceeding the size thresholds defined in claude-meta `best-practices/DOCS.md`. See that guide for the simplification methodology.

**Thresholds:**
- Python scripts/modules: 300 lines
- Test files: 400 lines

## Status

Last reviewed: 2026-04-10 (Phase 40 — patterns.py and history.py splits complete)

The following files exceed thresholds. They are organized as **one file per check category**, which is the project's intentional structure (each check module groups related AST/cross-file checks).

## Long check functions (suppressed inline pending refactor)

All three previously-suppressed functions were refactored during Phase 40 (sub-session A). No `# pysmelly: ignore[long-function]` suppressions remain.

| Function | Original file | Resolution |
|----------|--------------|------------|
| `check_temp_accumulators` | `patterns.py` | Refactored into helpers in `patterns_misc.py` |
| `check_fossilized_toggles` | `patterns.py` | Refactored into helpers in `patterns_misc.py` |
| `check_law_of_demeter` | `patterns.py` | Refactored into helpers in `patterns_misc.py` |

## Source files exceeding 300 lines

| File | Lines | Status | Notes |
|------|------:|--------|-------|
| `src/pysmelly/checks/patterns_misc.py` | 1292 | Watch | 9 checks (remainder after split). Largest sub-module but coherent. Split if it grows past ~1500. |
| `src/pysmelly/checks/callers.py` | 1326 | Watch | 10 cross-file call-graph checks. Currently coherent (all about call-graph). Split if it grows past ~1500. |
| `src/pysmelly/checks/structure.py` | 1052 | Watch | 11 structural checks. Currently coherent. Split if it grows past ~1500. |
| `src/pysmelly/cli.py` | 1041 | Watch | Click subcommands + suppression logic + output routing. Could extract `cli_suppression.py` (handles `# pysmelly: ignore` parsing) and `cli_init.py` (init subcommand for guidance file). |
| `src/pysmelly/checks/repetition.py` | 913 | Watch | 4 repetition checks but each is large (scattered-constants, shotgun-surgery, repeated-string-parsing). Currently fine. |
| `src/pysmelly/checks/architecture.py` | 822 | Watch | 8 architectural checks. Currently fine. |
| `src/pysmelly/checks/history_coupling.py` | 627 | Watch | 7 coupling/debt checks. Largest history sub-module but coherent. |
| `src/pysmelly/git_history.py` | 602 | Acceptable | Single coherent module: git log parsing + commit classification. Above 300 but cohesive. |
| `src/pysmelly/checks/dead.py` | 472 | Acceptable | 5 dead code extension checks. Above 300 but coherent. |
| `src/pysmelly/checks/helpers.py` | 461 | Acceptable | Shared AST utilities. Above 300 but coherent. |
| `src/pysmelly/checks/patterns_data.py` | 454 | Acceptable | 4 data pattern checks. Coherent. |
| `src/pysmelly/checks/patterns_naming.py` | 396 | Acceptable | 4 naming pattern checks. Coherent. |

## Test files exceeding 400 lines

| File | Lines | Status | Notes |
|------|------:|--------|-------|
| `tests/test_callers.py` | 1697 | Watch | Mirrors callers.py |
| `tests/test_structure.py` | 1069 | Watch | Mirrors structure.py |
| `tests/test_patterns_misc.py` | 1068 | Watch | Mirrors patterns_misc.py |
| `tests/test_dead.py` | 857 | Watch | Mirrors dead.py |
| `tests/test_architecture.py` | 778 | Watch | Mirrors architecture.py |
| `tests/test_history_coupling.py` | 774 | Watch | Mirrors history_coupling.py |
| `tests/test_repetition.py` | 759 | Watch | Mirrors repetition.py |
| `tests/test_history_team.py` | 598 | Watch | Mirrors history_team.py |
| `tests/test_cli.py` | 570 | Watch | CLI behavior tests |
| `tests/test_patterns_data.py` | 545 | Watch | Mirrors patterns_data.py |
| `tests/test_git_history.py` | 449 | Watch | Just over threshold, coherent |
| `tests/test_history_bugs.py` | 437 | Watch | Mirrors history_bugs.py |

## Resolved candidates

| File | Original lines | Resolution | Date |
|------|---------------:|------------|------|
| `src/pysmelly/checks/patterns.py` | 2411 | Split into `patterns_data.py`, `patterns_control.py`, `patterns_naming.py`, `patterns_misc.py` | 2026-04-09 |
| `src/pysmelly/checks/history.py` | 1410 | Split into `history_helpers.py`, `history_growth.py`, `history_bugs.py`, `history_team.py`, `history_coupling.py` | 2026-04-10 |
| `tests/test_patterns.py` | 2253 | Split into 4 test files matching source modules | 2026-04-09 |
| `tests/test_history_checks.py` | 2225 | Split into 4 test files + `history_test_helpers.py` | 2026-04-10 |

## Methodology Notes

The project's "one file per check category" organization is intentional and documented in `docs/DECISIONS.md`. The sub-category splits above preserve this principle (e.g., `patterns_data.py`, `patterns_control.py`, etc.).

Before splitting any file:
1. Read `claude-meta/best-practices/DOCS.md` Practice 1 for the simplification methodology
2. Run tests before and after
3. Make small focused commits
4. Don't change check behavior — only file organization
5. Update `src/pysmelly/checks/__init__.py` to import the new modules (so registration still works)
6. Update `CLAUDE.md` "Key Files" section to reflect the new structure
