# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [2.2.0] - 2026-07-02

### Added
- Workspace & endpoint registry with CRUD in harness-config.json
- CLI commands: `cap config workspaces list/add/remove`, `cap config endpoints list/add/remove`
- Auto-registration of workspaces on session_start
- Daemon periodic sync of all registered workspaces
- KB enforcement hooks: grep/find/rg blocked unless knowledge_search called first
- Passthrough mode for enforcement bypass
- Violation recording and audit trail
- `model_alias` field in cap_route response
- Hot-reload of budget from harness-config.json in cap_status
- Architecture keyword weighting (0.55) in router
- Trivial question negative weighting (-0.2) in router

### Changed
- Standardized `ssh_endpoint` field naming (was `ssh_url_template`)
- Router scoring: added architecture_keywords and trivial_question_keywords signals
- PreToolUse hooks simplified to `python3 ~/.claude/pretool.py` (Claude Code pipes stdin)

### Fixed
- False positive grep detection on commands containing "find" in arguments
- pyarrow version constraint widened for Python 3.13 compatibility
- Region detection: reads from AWS profile config, defaults eu-central-1
- Workflow store mark_failed_stale returns proper rowcount

## [2.1.0] - 2026-06-30

### Added
- 3-tier adaptive complexity routing (ADR-011)
- Hard enforcement via PreToolUse exit code 2 (ADR-009)
- Professional README with SVG banner
- Full 37-scenario test suite (100% pass)
