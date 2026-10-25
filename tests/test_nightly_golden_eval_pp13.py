"""Tests for PP13 nightly golden eval functionality."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from clickhouse_driver import Client

from agent_obs.storage.hot import HotStore
from scripts.nightly_golden_eval import (
    GoldenCase,
    compare_scores,
    load_golden_set,
    run_nightly_golden_eval,
)


@pytest.fixture
def mock_clickhouse() -> AsyncMock:
    """Mock ClickHouse client for testing."""
    mock_client = AsyncMock(spec=Client)
    mock_client.execute = AsyncMock()
    return mock_client


@pytest.fixture
def mock_hot_store(mock_clickhouse: AsyncMock) -> HotStore:
    """Mock HotStore with ClickHouse client."""
    with patch("clickhouse_driver.Client", return_value=mock_clickhouse):
        return HotStore(host="localhost", port=9000)


@pytest.fixture
def golden_set_path() -> Path:
    """Path to test golden set file."""
    return Path(__file__).parent.parent / "eval" / "golden" / "capitals.yaml"


@pytest.fixture
def sample_golden_set() -> list[GoldenCase]:
    """Sample golden set for testing."""
    return [
        GoldenCase(
            id="capital_of_france",
            question="What is the capital of France?",
            expected_answer="The capital of France is Paris.",
            expected_scores={
                "faithfulness": 0.85,
                "answer_relevancy": 0.85,
                "completeness": 0.75,
            },
        ),
        GoldenCase(
            id="capital_of_japan",
            question="What is the capital of Japan?",
            expected_answer="The capital of Japan is Tokyo.",
            expected_scores={
                "faithfulness": 0.85,
                "answer_relevancy": 0.85,
                "completeness": 0.75,
            },
        ),
    ]


class TestLoadGoldenSet:
    """Test loading and parsing of golden set files."""

    def test_load_valid_golden_set(self, golden_set_path: Path) -> None:
        """Test loading a valid golden set file."""
        # Pass the directory containing the golden set files
        golden_dir = golden_set_path.parent
        golden_set = load_golden_set(str(golden_dir))
        
        assert len(golden_set) == 5
        
        # Check first case
        case = golden_set[0]
        assert case.id == "capital_of_france"
        assert case.question == "What is the capital of France?"
        assert case.expected_answer == "The capital of France is Paris."
        assert case.expected_scores == {
            "faithfulness": 0.85,
            "answer_relevancy": 0.85,
            "completeness": 0.75,
        }

    def test_load_nonexistent_file(self) -> None:
        """Test loading a non-existent directory raises ValueError."""
        with pytest.raises(ValueError, match="golden set directory not found"):
            load_golden_set("nonexistent_dir")

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        """Test loading invalid YAML raises ValueError."""
        # Create a directory with invalid YAML file
        golden_dir = tmp_path / "golden"
        golden_dir.mkdir()
        invalid_file = golden_dir / "invalid.yaml"
        invalid_file.write_text("invalid: yaml: content: [")
    
        with pytest.raises((ValueError, yaml.scanner.ScannerError), match="mapping values are not allowed here"):
            load_golden_set(str(golden_dir))

    def test_load_missing_required_fields(self, tmp_path: Path) -> None:
        """Test loading YAML with missing required fields."""
        golden_dir = tmp_path / "golden"
        golden_dir.mkdir()
        invalid_file = golden_dir / "invalid.yaml"
        invalid_file.write_text("""
golden_cases:
  - id: "test-001"
    # Missing question and expected_answer
""")

        with pytest.raises(ValueError, match="missing string 'question'"):
            load_golden_set(str(golden_dir))

    def test_load_duplicate_ids(self, tmp_path: Path) -> None:
        """Test loading YAML with duplicate IDs raises ValueError."""
        golden_dir = tmp_path / "golden"
        golden_dir.mkdir()
        duplicate_file = golden_dir / "duplicate.yaml"
        duplicate_file.write_text("""
golden_cases:
  - id: "test-001"
    question: "Q1"
    expected_answer: "A1"
    expected_scores: {}
  - id: "test-001"
    question: "Q2"
    expected_answer: "A2"
    expected_scores: {}
""")

        with pytest.raises(ValueError, match="missing non-empty 'expected_scores'"):
            load_golden_set(str(golden_dir))

    def test_load_unsupported_metric(self, tmp_path: Path) -> None:
        """Test loading YAML with unsupported metric raises ValueError."""
        golden_dir = tmp_path / "golden"
        golden_dir.mkdir()
        invalid_file = golden_dir / "invalid.yaml"
        invalid_file.write_text("""
golden_cases:
  - id: "test-001"
    question: "Q1"
    expected_answer: "A1"
    expected_scores:
      unsupported_metric: 0.8
""")

        with pytest.raises(ValueError, match="unsupported metric"):
            load_golden_set(str(golden_dir))


class TestCompareScores:
    """Test score comparison logic."""

    def test_all_metrics_pass(self, sample_golden_set: list[GoldenCase]) -> None:
        """Test case where all metrics pass."""
        scores = {
            "faithfulness": 0.9,
            "answer_relevancy": 0.9,
            "completeness": 0.8,
        }
        
        result = compare_scores(scores, sample_golden_set[0].expected_scores)
        
        assert result is True

    def test_one_metric_fails(self, sample_golden_set: list[GoldenCase]) -> None:
        """Test case where one metric fails."""
        scores = {
            "faithfulness": 0.8,  # Below threshold 0.85
            "answer_relevancy": 0.9,
            "completeness": 0.8,
        }
        
        result = compare_scores(scores, sample_golden_set[0].expected_scores)
        
        assert result is False

    def test_all_metrics_fail(self, sample_golden_set: list[GoldenCase]) -> None:
        """Test case where all metrics fail."""
        scores = {
            "faithfulness": 0.5,
            "answer_relevancy": 0.6,
            "completeness": 0.7,
        }
        
        result = compare_scores(scores, sample_golden_set[0].expected_scores)
        
        assert result is False

    def test_edge_case_exact_threshold(self, sample_golden_set: list[GoldenCase]) -> None:
        """Test case where score equals threshold exactly."""
        scores = {
            "faithfulness": 0.85,  # Exactly at threshold
            "answer_relevancy": 0.85,
            "completeness": 0.75,
        }
        
        result = compare_scores(scores, sample_golden_set[0].expected_scores)
        
        assert result is True


class TestRunNightlyGoldenEval:
    """Test the main nightly golden eval function."""

    @patch("scripts.nightly_golden_eval.build_agent_runner")
    @patch("scripts.nightly_golden_eval.build_judge_client")
    async def test_run_nightly_golden_eval_success(
        self,
        mock_build_judge: AsyncMock,
        mock_build_agent: AsyncMock,
        mock_hot_store: HotStore,
        sample_golden_set: list[GoldenCase],
    ) -> None:
        """Test successful run of nightly golden eval."""
        # Load the actual golden set instead of using the fixture
        actual_golden_set = load_golden_set(str(Path(__file__).parent.parent / "eval" / "golden"))
        
        # Mock golden set loading
        with patch("scripts.nightly_golden_eval.load_golden_set", return_value=actual_golden_set):
            # Create a mock agent function that returns the answer directly
            mock_agent_func = AsyncMock(return_value="Paris is the capital of France.")
            
            # Mock build_agent_runner to return our mock agent function
            mock_build_agent.return_value = mock_agent_func
            
            # Mock judge client
            mock_judge = AsyncMock()
            mock_judge.chat.completions.create = AsyncMock(
                return_value=SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps({
                                "score": 0.95,
                                "reasoning": "Good answer",
                                "flags": []
                            })
                        )
                    )]
                )
            )
            mock_build_judge.return_value = mock_judge
            
            # Mock HotStore write method
            mock_hot_store.write_eval_golden_results = MagicMock(return_value=5)
            
            # Run the eval
            result = await run_nightly_golden_eval(
                dry_run=False,
                golden_dir=str(Path(__file__).parent.parent / "eval" / "golden"),
                hot_store=mock_hot_store,
            )
            
            # Verify results
            assert result.pass_rate == pytest.approx(1.0, rel=1e-3)
            assert result.total == 5
            assert result.passed == 5
            assert len(result.case_results) == 5
            
            # Verify agent and judge were called
            assert mock_agent_func.call_count == 5
            assert mock_judge.chat.completions.create.call_count == 15  # 5 cases * 3 metrics each
            
            # Verify ClickHouse write
            mock_hot_store.write_eval_golden_results.assert_called_once()
            call_args = mock_hot_store.write_eval_golden_results.call_args
            assert len(call_args[0][0]) == 15  # 5 cases * 3 metrics each
            for row in call_args[0][0]:
                assert "nightly_ts" in row
                assert "question_id" in row
                assert "metric_name" in row
                assert "metric_name" in row
                assert "expected_score" in row
                assert "actual_score" in row
                assert "pass" in row

    @patch("scripts.nightly_golden_eval.build_agent_runner")
    @patch("scripts.nightly_golden_eval.build_judge_client")
    async def test_run_nightly_golden_eval_with_failures(
        self,
        mock_build_judge: AsyncMock,
        mock_build_agent: AsyncMock,
        mock_hot_store: HotStore,
        sample_golden_set: list[GoldenCase],
    ) -> None:
        """Test run with some failed cases."""
        # Load the actual golden set instead of using the fixture
        actual_golden_set = load_golden_set(str(Path(__file__).parent.parent / "eval" / "golden"))
        
        with patch("scripts.nightly_golden_eval.load_golden_set", return_value=actual_golden_set):
            # Create a mock agent function that returns the answer directly
            mock_agent_func = AsyncMock(return_value="Paris is the capital of France.")
            
            # Mock build_agent_runner to return our mock agent function
            mock_build_agent.return_value = mock_agent_func
            
            # Mock judge client with mixed results
            mock_judge = AsyncMock()
            mock_judge.chat.completions.create = AsyncMock(
                side_effect=[
                    # First case: pass (3 metrics)
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    # Second case: fail (low faithfulness)
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.6,
                                    "reasoning": "Contains hallucinated information",
                                    "flags": ["faithfulness"]
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    # Third case: pass (3 metrics)
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.9,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.9,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.9,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    # Fourth case: pass (3 metrics)
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    # Fifth case: pass (3 metrics)
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                    SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "Good answer",
                                    "flags": []
                                })
                            )
                        )]
                    ),
                ]
            )
            mock_build_judge.return_value = mock_judge
            
            # Mock HotStore write method
            mock_hot_store.write_eval_golden_results = MagicMock(return_value=15)
            
            # Run the eval
            result = await run_nightly_golden_eval(
                dry_run=False,
                golden_dir=str(Path(__file__).parent.parent / "eval" / "golden"),
                hot_store=mock_hot_store,
            )
            
            # Verify results - should be 4 passed out of 5 (one case failed)
            assert result.pass_rate == pytest.approx(0.8, rel=1e-3)  # 4/5 = 0.8
            assert result.total == 5
            assert result.passed == 4
            assert result.total - result.passed == 1
            assert len(result.case_results) == 5
            
            # Verify agent and judge were called
            assert mock_agent_func.call_count == 5
            assert mock_judge.chat.completions.create.call_count == 15  # 5 cases * 3 metrics each
            
            # Verify ClickHouse write
            mock_hot_store.write_eval_golden_results.assert_called_once()
            call_args = mock_hot_store.write_eval_golden_results.call_args
            assert len(call_args[0][0]) == 15  # 5 cases * 3 metrics each
            for row in call_args[0][0]:
                assert "nightly_ts" in row
                assert "question_id" in row
                assert "metric_name" in row

    @patch("scripts.nightly_golden_eval.build_agent_runner")
    async def test_run_nightly_golden_eval_dry_run(
        self,
        mock_build_agent: AsyncMock,
        mock_hot_store: HotStore,
        sample_golden_set: list[GoldenCase],
    ) -> None:
        """Test dry run mode (no API calls, no ClickHouse writes)."""
        with patch("scripts.nightly_golden_eval.load_golden_set", return_value=sample_golden_set), \
             patch("scripts.nightly_golden_eval._build_dry_run_judge_client") as mock_dry_run:
                # Mock dry run judge
                mock_judge = AsyncMock()
                mock_judge.chat.completions.create = AsyncMock(
                    return_value=SimpleNamespace(
                        choices=[SimpleNamespace(
                            message=SimpleNamespace(
                                content=json.dumps({
                                    "score": 0.95,
                                    "reasoning": "dry-run",
                                    "flags": []
                                })
                            )
                        )]
                    )
                )
                mock_dry_run.return_value = mock_judge
                
                # Mock agent runner (should not be called in dry run)
                mock_agent = AsyncMock()
                mock_build_agent.return_value = mock_agent
                
                # Run the eval in dry run mode
                result = await run_nightly_golden_eval(
                    dry_run=True,
                    golden_dir=str(Path(__file__).parent.parent / "eval" / "golden"),
                    hot_store=mock_hot_store,
                )
                
                # Verify results
                assert result.pass_rate == pytest.approx(1.0, rel=1e-3)
                assert result.total == 2
                assert result.passed == 2
                
                # Verify no API calls were made
                mock_agent.run.assert_not_called()

    @patch("scripts.nightly_golden_eval.build_agent_runner")
    async def test_run_nightly_golden_eval_agent_failure(
        self,
        mock_build_agent: AsyncMock,
        mock_hot_store: HotStore,
        sample_golden_set: list[GoldenCase],
    ) -> None:
        """Test run when agent call fails."""
        # Load the actual golden set instead of using the fixture
        actual_golden_set = load_golden_set(str(Path(__file__).parent.parent / "eval" / "golden"))
        
        with patch("scripts.nightly_golden_eval.load_golden_set", return_value=actual_golden_set):
            # Create a mock agent function that fails
            mock_agent_func = AsyncMock(side_effect=Exception("Agent failed"))
            
            # Mock build_agent_runner to return our mock agent function
            mock_build_agent.return_value = mock_agent_func
            
            # Mock HotStore write method
            mock_hot_store.write_eval_golden_results = MagicMock(return_value=0)
            
            # Run the eval
            result = await run_nightly_golden_eval(
                dry_run=False,
                golden_dir=str(Path(__file__).parent.parent / "eval" / "golden"),
                hot_store=mock_hot_store,
            )
            
            # Verify results (all cases failed due to agent error)
            assert result.pass_rate == 0.0
            assert result.total == 5
            assert result.passed == 0
            assert result.total - result.passed == 5
            assert len(result.case_results) == 5
            assert len(result.errors) == 5  # All cases should have errors
            
            # Verify agent was called but judge was not
            assert mock_agent_func.call_count == 5
            # Mock judge client should not be called since agent failed first


class TestIntegration:
    """Integration tests with ClickHouse and Prometheus."""

    def test_prometheus_rule_yaml_structure(self) -> None:
        """Test that the Prometheus rule YAML contains the expected structure."""
        rules_path = Path(__file__).parent.parent / "infra" / "prometheus-rules.yml"
        rules_content = rules_path.read_text()
        
        # Check golden-eval group exists
        assert "- name: golden-eval" in rules_content
        
        # Check GoldenEvalRegression rule
        assert "GoldenEvalRegression" in rules_content
        assert "agent_obs_eval_golden_pass_rate" in rules_content
        assert "avg_over_time(agent_obs_eval_golden_pass_rate[7d]) - 0.10" in rules_content
        
        # Check GoldenEvalJobStale rule
        assert "GoldenEvalJobStale" in rules_content
        assert "nightly_golden_eval" in rules_content

    def test_clickhouse_ddl_contains_eval_golden_results(self) -> None:
        """Test that ClickHouse DDL contains eval_golden_results table."""
        ddl_path = Path(__file__).parent.parent / "infra" / "storage" / "clickhouse_ddl.sql"
        ddl_content = ddl_path.read_text()
        
        assert "eval_golden_results" in ddl_content
        assert "nightly_ts" in ddl_content
        assert "question_id" in ddl_content
        assert "metric_name" in ddl_content
        assert "expected_score" in ddl_content
        assert "actual_score" in ddl_content
        assert "pass" in ddl_content

    def test_scheduler_registers_nightly_golden_eval_job(self) -> None:
        """Test that the scheduler registers the nightly golden eval job."""
        scheduler_path = Path(__file__).parent.parent / "scripts" / "cron" / "scheduler.py"
        scheduler_content = scheduler_path.read_text()
        
        # Check job constant
        assert "JOB_NIGHTLY_GOLDEN_EVAL" in scheduler_content
        
        # Check job function
        assert "_nightly_golden_eval_job" in scheduler_content
        
        # Check default spec
        assert "DAILY_GOLDEN_EVAL_SPEC" in scheduler_content
        assert "0 3 * * *" in scheduler_content
        
        # Check CronJob registration
        assert "nightly_golden_eval" in scheduler_content


if __name__ == "__main__":
    pytest.main([__file__])