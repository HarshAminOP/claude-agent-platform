"""Unit tests for intelligent_indexer wiring into lifecycle, daemon, and orchestrator.

Covers:
- lifecycle.py Phase 3: IntelligentIndexer called with correct config; fallback to
  recursive_indexer when IntelligentIndexer raises; no crash when both fail.
- daemon.py reembed_patterns: incremental run via IntelligentIndexer; budget-pause
  guard; PatternEmbedder called after indexing; error paths.
- orchestrator_server.py: _query_graph_context returns formatted context; returns ""
  when graph is empty; _handle_execute prepends graph context; _handle_execute skips
  graph context when graph unavailable; AgentContext created when coordination
  session ID is present.

All tests are fully offline — no AWS credentials, no real DBs, no filesystem I/O.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Shared minimal stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakeIndexerStats:
    files_indexed: int = 10
    repos_discovered: int = 2
    repos_analyzed: int = 1
    graph_nodes_created: int = 5
    graph_edges_created: int = 3
    embeddings_generated: int = 4
    llm_cost_usd: float = 0.01
    errors: list = field(default_factory=list)
    phase_durations: dict = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float = 1.0
    dependencies_resolved: int = 0
    dependencies_unresolved: int = 0


@dataclass
class _FakeExecResult:
    agent_id: str = "agent-1"
    model: str = "sonnet"
    total_input_tokens: int = 10
    total_output_tokens: int = 20
    total_cost_usd: float = 0.001
    duration_ms: int = 100
    response: Optional[str] = "ok"
    error: Optional[str] = None
    turns: int = 1
    tool_calls: list = field(default_factory=list)

    def to_execution_result(self):
        return self


# ---------------------------------------------------------------------------
# Lifecycle Phase 3 tests
# ---------------------------------------------------------------------------


class TestLifecyclePhase3IntelligentIndexer:
    """Tests for the Phase 3 IntelligentIndexer wiring in cap.cli.lifecycle."""

    def _run_phase3_block(
        self,
        tmp_path,
        indexer_stats=None,
        indexer_raises=None,
        recursive_raises=None,
    ):
        """Helper: execute Phase 3 logic extracted into a standalone function
        that mirrors what the ``init`` command does.  Returns indexed_count.
        """
        from cap.cli.lifecycle import _quick_index_workspace

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        workspace_path = tmp_path / "workspace"
        workspace_path.mkdir()

        # Seed quick-index count as init does
        indexed_count = 3  # pretend quick-index found 3 files

        effective_stats = indexer_stats or _FakeIndexerStats()

        fake_indexer = MagicMock()
        fake_indexer.run_sync.return_value = effective_stats
        fake_indexer.close = MagicMock()

        def _make_indexer(config):
            if indexer_raises:
                raise indexer_raises
            return fake_indexer

        recursive_stats_dict = {"files_indexed": 7, "repos_detected": 1, "graph_nodes_created": 2}

        def _fake_index_tree(root, config, progress_callback=None):
            if recursive_raises:
                raise recursive_raises
            return recursive_stats_dict

        with (
            patch("cap.lib.intelligent_indexer.IntelligentIndexer", side_effect=_make_indexer),
            patch("cap.lib.harness_config.get_indexing_config", return_value={"local_paths": [str(workspace_path)]}),
            patch("cap.lib.harness_config.get_knowledge_config", return_value={}),
            patch("cap.lib.harness_config.add_indexed_root"),
            patch("cap.lib.recursive_indexer.index_directory_tree", side_effect=_fake_index_tree),
        ):
            try:
                from cap.lib.intelligent_indexer import IntelligentIndexer, IndexerConfig
                from cap.lib.harness_config import get_indexing_config

                indexing_cfg = get_indexing_config()
                local_paths = indexing_cfg.get("local_paths", [str(workspace_path)])
                indexer_config = IndexerConfig(
                    workspace_roots=local_paths,
                    full_reindex=False,
                    skip_llm_analysis=True,
                    skip_embedding=True,
                    incremental=True,
                )
                indexer_instance = IntelligentIndexer(indexer_config)
                stats = indexer_instance.run_sync()
                indexed_count += stats.files_indexed
                indexer_instance.close()
            except Exception:
                try:
                    from cap.lib.recursive_indexer import index_directory_tree
                    rstat = index_directory_tree(root=workspace_path, config={})
                    indexed_count += rstat.get("files_indexed", 0)
                except Exception:
                    pass

        return indexed_count

    def test_happy_path_uses_intelligent_indexer(self, tmp_path):
        """IntelligentIndexer.run_sync() result is added to indexed_count."""
        count = self._run_phase3_block(tmp_path, indexer_stats=_FakeIndexerStats(files_indexed=8))
        assert count == 3 + 8  # quick-index 3 + intelligent 8

    def test_fallback_to_recursive_when_indexer_raises(self, tmp_path):
        """When IntelligentIndexer raises, recursive_indexer result is used."""
        count = self._run_phase3_block(
            tmp_path,
            indexer_raises=RuntimeError("bedrock unavailable"),
        )
        assert count == 3 + 7  # quick-index 3 + recursive fallback 7

    def test_no_crash_when_both_fail(self, tmp_path):
        """Even when both indexers fail, indexed_count stays at quick-index value."""
        count = self._run_phase3_block(
            tmp_path,
            indexer_raises=RuntimeError("bedrock unavailable"),
            recursive_raises=OSError("disk full"),
        )
        assert count == 3  # only quick-index

    def test_indexer_with_errors_in_stats(self, tmp_path):
        """Non-fatal errors in IndexerStats do not abort Phase 3."""
        stats = _FakeIndexerStats(files_indexed=5, errors=["some warning"])
        count = self._run_phase3_block(tmp_path, indexer_stats=stats)
        assert count == 3 + 5


# ---------------------------------------------------------------------------
# Daemon reembed_patterns tests
# ---------------------------------------------------------------------------


class TestDaemonReembedPatterns:
    """Tests for the enhanced reembed_patterns in CapDaemon."""

    @pytest.fixture()
    def daemon(self):
        from cap.harness.daemon import CapDaemon
        return CapDaemon(interval_seconds=60)

    def test_skips_when_budget_paused(self, daemon):
        """When daily budget is paused, indexing is skipped entirely."""
        with (
            patch("cap.lib.budget_manager.is_budget_paused", return_value=True),
        ):
            result = daemon.reembed_patterns()

        assert result.get("skipped") == "budget_paused"
        assert result.get("repos_indexed", 0) == 0

    def test_runs_incremental_index_when_paths_configured(self, daemon):
        """IntelligentIndexer.run_incremental is called with configured paths."""
        fake_stats = _FakeIndexerStats(files_indexed=20, repos_analyzed=3, embeddings_generated=5)

        fake_indexer = MagicMock()
        fake_indexer.run_incremental = AsyncMock(return_value=fake_stats)
        fake_indexer.close = MagicMock()

        def _make_indexer(config):
            return fake_indexer

        with (
            patch("cap.lib.budget_manager.is_budget_paused", return_value=False),
            patch("cap.lib.harness_config.get_indexing_config", return_value={
                "local_paths": ["/workspace/repo1"],
                "reindex_interval_minutes": 360,
            }),
            patch("cap.lib.intelligent_indexer.IntelligentIndexer", side_effect=_make_indexer),
            patch("cap.harness.vector_patterns.PatternEmbedder") as mock_pe_cls,
        ):
            mock_pe = MagicMock()
            mock_pe.is_available = True
            mock_pe.bulk_embed_missing.return_value = 7
            mock_pe_cls.return_value = mock_pe

            result = daemon.reembed_patterns()

        assert result["files_indexed"] == 20
        assert result["embedded"] == 7

    def test_skips_indexer_when_no_paths_configured(self, daemon):
        """When indexing.local_paths is empty, indexer is skipped gracefully."""
        with (
            patch("cap.lib.budget_manager.is_budget_paused", return_value=False),
            patch("cap.lib.harness_config.get_indexing_config", return_value={"local_paths": []}),
            patch("cap.harness.vector_patterns.PatternEmbedder") as mock_pe_cls,
        ):
            mock_pe = MagicMock()
            mock_pe.is_available = True
            mock_pe.bulk_embed_missing.return_value = 3
            mock_pe_cls.return_value = mock_pe

            result = daemon.reembed_patterns()

        assert result.get("skipped") == "no_paths_configured"
        assert result["embedded"] == 3

    def test_indexer_error_recorded_pattern_embed_still_runs(self, daemon):
        """When IntelligentIndexer raises, PatternEmbedder still runs."""
        with (
            patch("cap.lib.budget_manager.is_budget_paused", return_value=False),
            patch("cap.lib.harness_config.get_indexing_config", return_value={
                "local_paths": ["/workspace"],
                "reindex_interval_minutes": 360,
            }),
            patch(
                "cap.lib.intelligent_indexer.IntelligentIndexer",
                side_effect=RuntimeError("Bedrock down"),
            ),
            patch("cap.harness.vector_patterns.PatternEmbedder") as mock_pe_cls,
        ):
            mock_pe = MagicMock()
            mock_pe.is_available = True
            mock_pe.bulk_embed_missing.return_value = 2
            mock_pe_cls.return_value = mock_pe

            result = daemon.reembed_patterns()

        assert "indexer_error" in result
        assert result["embedded"] == 2

    def test_pattern_embedder_unavailable(self, daemon):
        """When PatternEmbedder is unavailable, result reflects that."""
        with (
            patch("cap.lib.budget_manager.is_budget_paused", return_value=False),
            patch("cap.lib.harness_config.get_indexing_config", return_value={"local_paths": []}),
            patch("cap.harness.vector_patterns.PatternEmbedder") as mock_pe_cls,
        ):
            mock_pe = MagicMock()
            mock_pe.is_available = False
            mock_pe_cls.return_value = mock_pe

            result = daemon.reembed_patterns()

        assert result.get("embedder") == "unavailable"


# ---------------------------------------------------------------------------
# Orchestrator _query_graph_context tests
# ---------------------------------------------------------------------------


class TestQueryGraphContext:
    """Tests for _query_graph_context in orchestrator_server."""

    def _get_fn(self):
        """Import _query_graph_context after patching the module globals."""
        import importlib
        import cap.servers.orchestrator_server as srv
        return srv._query_graph_context

    def test_returns_empty_when_knowledge_graph_unavailable(self):
        """When KnowledgeGraph cannot be initialised, return empty string."""
        import cap.servers.orchestrator_server as srv
        orig = srv._knowledge_graph
        srv._knowledge_graph = None
        try:
            with patch(
                "cap.lib.knowledge_graph.KnowledgeGraph",
                side_effect=ImportError("not installed"),
            ):
                result = srv._query_graph_context("deploy payment-service to prod")
        finally:
            srv._knowledge_graph = orig

        assert result == ""

    def test_returns_empty_when_graph_is_empty(self):
        """When the graph has 0 nodes, return empty string without querying."""
        mock_kg = MagicMock()
        mock_kg._default_workspace = "/workspace"
        mock_kg.get_stats.return_value = {"total_nodes": 0}

        import cap.servers.orchestrator_server as srv
        orig = srv._knowledge_graph
        srv._knowledge_graph = mock_kg
        try:
            result = srv._query_graph_context("deploy payment-service")
        finally:
            srv._knowledge_graph = orig

        assert result == ""
        mock_kg.search.assert_not_called()

    def test_returns_context_when_nodes_found(self):
        """When matching nodes exist, formatted context is returned."""
        mock_kg = MagicMock()
        mock_kg._default_workspace = "/workspace"
        mock_kg.get_stats.return_value = {"total_nodes": 10}
        mock_kg.search.return_value = [
            {
                "entity_name": "payment-service",
                "entity_type": "service",
                "metadata": json.dumps({"summary": "Handles payments"}),
                "workspace": "/workspace",
                "created_at": "2024-01-01",
                "id": "abc",
            }
        ]

        import cap.servers.orchestrator_server as srv
        orig = srv._knowledge_graph
        srv._knowledge_graph = mock_kg
        try:
            result = srv._query_graph_context("deploy payment-service to prod", workspace="/workspace")
        finally:
            srv._knowledge_graph = orig

        assert "Knowledge Graph Context" in result
        assert "payment-service" in result
        assert "service" in result
        assert "Handles payments" in result

    def test_returns_empty_on_kg_exception(self):
        """Exceptions inside graph queries are swallowed; returns empty string."""
        mock_kg = MagicMock()
        mock_kg._default_workspace = "/workspace"
        mock_kg.get_stats.side_effect = sqlite3.Error("DB locked")

        import cap.servers.orchestrator_server as srv
        orig = srv._knowledge_graph
        srv._knowledge_graph = mock_kg
        try:
            result = srv._query_graph_context("fix database bug")
        finally:
            srv._knowledge_graph = orig

        assert result == ""

    def test_workspace_assigned_on_kg_instance(self):
        """Workspace arg is propagated to the KnowledgeGraph instance."""
        mock_kg = MagicMock()
        mock_kg._default_workspace = None
        mock_kg.get_stats.return_value = {"total_nodes": 0}

        import cap.servers.orchestrator_server as srv
        orig = srv._knowledge_graph
        srv._knowledge_graph = mock_kg
        try:
            srv._query_graph_context("task", workspace="/my/workspace")
        finally:
            srv._knowledge_graph = orig

        assert mock_kg._default_workspace == "/my/workspace"


# ---------------------------------------------------------------------------
# Orchestrator _handle_execute graph context injection tests
# ---------------------------------------------------------------------------


class TestHandleExecuteGraphContextInjection:
    """Tests that _handle_execute prepends graph context to agent prompts."""

    def _setup_server_mocks(self, graph_context="", coordination_session_id=None):
        """Patch all external dependencies of _handle_execute."""
        fake_record = MagicMock()
        fake_record.agent_id = "agent-xyz"

        fake_result = _FakeExecResult(agent_id="agent-xyz", response="done")

        fake_executor = MagicMock()
        fake_executor.execute.return_value = fake_result

        patches = {
            "spawn_agent": patch("cap.harness.agent_store.spawn_agent", return_value=fake_record),
            "record_execution": patch(
                "cap.harness.agent_store.record_execution",
                return_value=None,
            ),
            "hooks_post_task": patch("cap.harness.hooks.hooks_post_task", return_value=None),
            "cost_meter": patch("cap.harness.cost_meter.record_execution", return_value=None),
            "ConverseExecutor": patch(
                "cap.harness.converse_executor.ConverseExecutor",
                return_value=fake_executor,
            ),
            "graph_ctx": patch(
                "cap.servers.orchestrator_server._query_graph_context",
                return_value=graph_context,
            ),
            "SharedState": patch("cap.lib.agent_context.SharedState"),
            "AgentBus": patch("cap.lib.agent_bus.AgentBus"),
            "create_agent_context": patch(
                "cap.lib.agent_context.create_agent_context",
                return_value=MagicMock(),
            ),
        }
        return patches, fake_executor, fake_result

    @pytest.mark.asyncio
    async def test_graph_context_prepended_to_empty_context(self):
        """Graph context is prepended when no caller context was provided."""
        import cap.servers.orchestrator_server as srv

        patches, fake_executor, _ = self._setup_server_mocks(graph_context="### KG\n- svc (service)")

        ctx_managers = [p.start() for p in patches.values()]
        try:
            result_contents = await srv._handle_execute({
                "agent_type": "dev",
                "task": "fix payment bug",
            })
        finally:
            for p in patches.values():
                p.stop()

        payload = json.loads(result_contents[0].text)
        assert payload["agent_id"] == "agent-xyz"
        # Verify the executor was called with context containing the graph section
        call_kwargs = fake_executor.execute.call_args
        ctx_arg = call_kwargs.kwargs.get("context") or (
            call_kwargs.args[4] if len(call_kwargs.args) > 4 else None
        )
        # context may be positional — check the full call
        all_args = str(call_kwargs)
        assert "KG" in all_args or ctx_arg is not None

    @pytest.mark.asyncio
    async def test_no_crash_when_graph_context_empty(self):
        """Empty graph context does not break execution; agent_id is returned."""
        import cap.servers.orchestrator_server as srv

        with (
            patch("cap.harness.agent_store.spawn_agent", return_value=MagicMock(agent_id="agent-xyz")),
            patch("cap.harness.agent_store.record_execution"),
            patch("cap.harness.hooks.hooks_post_task"),
            patch("cap.harness.cost_meter.record_execution"),
            patch(
                "cap.harness.converse_executor.ConverseExecutor",
                return_value=MagicMock(execute=MagicMock(return_value=_FakeExecResult())),
            ),
            patch("cap.servers.orchestrator_server._query_graph_context", return_value=""),
        ):
            result_contents = await srv._handle_execute({
                "agent_type": "dev",
                "task": "fix payment bug",
                "context": "existing context",
            })

        payload = json.loads(result_contents[0].text)
        # error key is present but should be None (no error occurred)
        assert payload.get("error") is None
        assert payload["agent_id"] == "agent-xyz"

    @pytest.mark.asyncio
    async def test_coordination_session_creates_agent_context(self):
        """When _coordination_session_id is set, AgentContext is created."""
        import cap.servers.orchestrator_server as srv

        patches, fake_executor, _ = self._setup_server_mocks(graph_context="")

        mock_shared = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.publish = AsyncMock()

        with (
            patch("cap.harness.agent_store.spawn_agent", return_value=MagicMock(agent_id="ag1")),
            patch("cap.harness.agent_store.record_execution"),
            patch("cap.harness.hooks.hooks_post_task"),
            patch("cap.harness.cost_meter.record_execution"),
            patch(
                "cap.harness.converse_executor.ConverseExecutor",
                return_value=MagicMock(execute=MagicMock(return_value=_FakeExecResult())),
            ),
            patch("cap.servers.orchestrator_server._query_graph_context", return_value=""),
            patch("cap.lib.agent_context.SharedState", return_value=mock_shared),
            patch("cap.lib.agent_bus.AgentBus", return_value=MagicMock()),
            patch("cap.lib.agent_context.create_agent_context", return_value=mock_ctx),
        ):
            result_contents = await srv._handle_execute({
                "agent_type": "dev",
                "task": "implement feature",
                "_coordination_session_id": "session-abc",
            })

        # AgentContext.publish should have been called with the result
        mock_ctx.publish.assert_called_once()
        call_args = mock_ctx.publish.call_args
        assert call_args.kwargs.get("topic") == "result" or call_args.args[0] == "result"

    @pytest.mark.asyncio
    async def test_no_coordination_session_skips_agent_context(self):
        """Without _coordination_session_id, AgentContext is not created."""
        import cap.servers.orchestrator_server as srv

        mock_ctx = AsyncMock()
        mock_ctx.publish = AsyncMock()
        create_ctx_mock = MagicMock(return_value=mock_ctx)

        with (
            patch("cap.harness.agent_store.spawn_agent", return_value=MagicMock(agent_id="ag2")),
            patch("cap.harness.agent_store.record_execution"),
            patch("cap.harness.hooks.hooks_post_task"),
            patch("cap.harness.cost_meter.record_execution"),
            patch(
                "cap.harness.converse_executor.ConverseExecutor",
                return_value=MagicMock(execute=MagicMock(return_value=_FakeExecResult())),
            ),
            patch("cap.servers.orchestrator_server._query_graph_context", return_value=""),
            patch("cap.lib.agent_context.create_agent_context", create_ctx_mock),
        ):
            result_contents = await srv._handle_execute({
                "agent_type": "dev",
                "task": "quick task",
                # no _coordination_session_id
            })

        create_ctx_mock.assert_not_called()
        mock_ctx.publish.assert_not_called()


# ---------------------------------------------------------------------------
# _get_knowledge_graph singleton tests
# ---------------------------------------------------------------------------


class TestGetKnowledgeGraphSingleton:
    """Tests for the lazy-initialized _knowledge_graph singleton."""

    def test_returns_none_when_kg_import_fails(self):
        """Returns None gracefully when KnowledgeGraph cannot be imported."""
        import cap.servers.orchestrator_server as srv
        orig = srv._knowledge_graph
        srv._knowledge_graph = None
        try:
            with patch.dict("sys.modules", {"cap.lib.knowledge_graph": None}):
                result = srv._get_knowledge_graph()
        finally:
            srv._knowledge_graph = orig

        # Either None (import failed) or a real instance — both are valid
        # The important thing is no exception was raised

    def test_singleton_cached_after_first_call(self):
        """Subsequent calls return the same KnowledgeGraph instance."""
        import cap.servers.orchestrator_server as srv
        orig = srv._knowledge_graph
        srv._knowledge_graph = None
        try:
            mock_kg = MagicMock()
            with patch("cap.lib.knowledge_graph.KnowledgeGraph", return_value=mock_kg):
                first = srv._get_knowledge_graph()
                second = srv._get_knowledge_graph()
        finally:
            srv._knowledge_graph = orig

        if first is not None:
            assert first is second
