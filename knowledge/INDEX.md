# Knowledge Base

A centralized, persistent brain for Claude Code. Available in ALL workspaces.

## Structure

```
~/.claude/knowledge/
├── INDEX.md          ← This file
├── repos/            ← Auto-indexed repo summaries
│   ├── _index.md    ← Master list of all repos
│   └── <Group>--<repo>.md
├── domains/          ← Architecture, patterns, concepts
│   └── <topic>.md
└── tasks/            ← Records of completed work
    └── <date>-<slug>.md
```

## How to Use

1. **Before any task** — check if relevant knowledge exists here first
2. **For repo context** — read `repos/_index.md` then specific repo files
3. **For architecture** — read `domains/<topic>.md`
4. **For prior work** — read `tasks/<date>-<slug>.md`

## How to Grow

- After significant tasks → write `tasks/<date>-<slug>.md`
- After learning domain concepts → write/update `domains/<topic>.md`
- After adding new repos → run `~/.claude/scripts/init-repo.sh` or `build-knowledge-base.sh`

## Contents

### Repos
Run `~/.claude/scripts/build-knowledge-base.sh` to populate.

### Domains
(Created as you work — architecture patterns, deployment flows, conventions)

### Tasks
(Created after significant completed work — decisions, outcomes, findings)
