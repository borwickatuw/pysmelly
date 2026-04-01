# pysmelly — Development Plan

See [PLAN-ARCHIVE.md](PLAN-ARCHIVE.md) for completed work (Phases 1-12), [DECISIONS.md](DECISIONS.md) for design decisions, [SOMEDAY-MAYBE.md](SOMEDAY-MAYBE.md) for future ideas.

## Phase 13: `pysmelly init --short` mode

Add a `--short` flag to `pysmelly init` that generates a ~15-line summary instead of the full 182-line guidance file. Key points only: severity tiers, "default is to fix", incremental workflow. Links to pysmelly's README for details.

**Why:** Cross-repo documentation consolidation (claude-meta Phase 34). The full PYSMELLY.md is identical across 6+ repos and drifts as pysmelly evolves. A short version preserves the CLAUDE.md pointer workflow while reducing per-repo footprint by ~90%.

**See:** claude-meta `docs/DECISIONS.md` "PYSMELLY.md consolidation approach"
