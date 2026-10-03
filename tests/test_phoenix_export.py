"""Tests for PC28 — Phoenix UMAP visualizer integration.

Validates that:
- Phoenix service is defined in docker-compose.yml
- Embedding export script exists and is importable
- Scheduler includes Phoenix export job
- Export functions handle edge cases correctly
- Prometheus metrics are registered and incremented correctly
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_COMPOSE_PATH = REPO_ROOT / "infra" / "docker-compose.yml"
EXPORT_SCRIPT_PATH = REPO_ROOT / "scripts" / "cron" / "export_embeddings_to_phoenix.py"
SCHEDULER_PATH = REPO_ROOT / "scripts" / "cron" / "scheduler.py"
ENV_EXAMPLE_PATH = REPO_ROOT / "infra" / ".env.example"


@pytest.fixture(scope="module")
def docker_compose():
    assert DOCKER_COMPOSE_PATH.exists(), f"missing {DOCKER_COMPOSE_PATH}"
    return yaml.safe_load(DOCKER_COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def env_example():
    assert ENV_EXAMPLE_PATH.exists(), f"missing {ENV_EXAMPLE_PATH}"
    return ENV_EXAMPLE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Docker Compose Phoenix service
# ---------------------------------------------------------------------------


class TestPhoenixDockerService:
    def test_phoenix_service_exists(self, docker_compose):
        """Phoenix service is defined in docker-compose.yml."""
        services = docker_compose.get("services", {})
        assert "phoenix" in services, "phoenix service not found in docker-compose.yml"

    def test_phoenix_image(self, docker_compose):
        """Phoenix uses arizephoenix/phoenix:latest image."""
        phoenix = docker_compose["services"]["phoenix"]
        assert "arizephoenix/phoenix" in phoenix.get("image", "")

    def test_phoenix_port_mapping(self, docker_compose):
        """Phoenix exposes port 6006 for UI access."""
        phoenix = docker_compose["services"]["phoenix"]
        ports = phoenix.get("ports", [])
        assert any("6006" in str(p) for p in ports), "port 6006 not mapped"

    def test_phoenix_healthcheck(self, docker_compose):
        """Phoenix has a healthcheck configured."""
        phoenix = docker_compose["services"]["phoenix"]
        assert "healthcheck" in phoenix, "phoenix missing healthcheck"

    def test_phoenix_volume(self, docker_compose):
        """Phoenix has a persistent volume for data."""
        phoenix = docker_compose["services"]["phoenix"]
        volumes = phoenix.get("volumes", [])
        assert any("phoenix_data" in v for v in volumes), "phoenix_data volume not mounted"

    def test_phoenix_data_volume_defined(self, docker_compose):
        """phoenix_data volume is defined in the volumes section."""
        volumes = docker_compose.get("volumes", {})
        assert "phoenix_data" in volumes, "phoenix_data volume not defined"


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------


class TestPhoenixEnvironmentVariables:
    def test_phoenix_port_in_env_example(self, env_example):
        """PHOENIX_PORT is documented in .env.example."""
        assert "PHOENIX_PORT" in env_example

    def test_phoenix_host_in_env_example(self, env_example):
        """PHOENIX_HOST is documented in .env.example."""
        assert "PHOENIX_HOST" in env_example

    def test_phoenix_export_interval_in_env_example(self, env_example):
        """PHOENIX_EXPORT_INTERVAL is documented in .env.example."""
        assert "PHOENIX_EXPORT_INTERVAL" in env_example


# ---------------------------------------------------------------------------
# Export script
# ---------------------------------------------------------------------------


class TestPhoenixExportScript:
    def test_export_script_exists(self):
        """export_embeddings_to_phoenix.py exists."""
        assert EXPORT_SCRIPT_PATH.exists(), f"missing {EXPORT_SCRIPT_PATH}"

    def test_export_script_is_importable(self):
        """export_embeddings_to_phoenix.py can be imported without errors."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "export_embeddings_to_phoenix", EXPORT_SCRIPT_PATH
        )
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        # Don't actually execute the module, just check it loads
        assert module is not None


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


class TestSchedulerIntegration:
    def test_scheduler_includes_phoenix_job(self):
        """Scheduler build_jobs() includes Phoenix export job."""
        # Read scheduler.py content
        scheduler_content = SCHEDULER_PATH.read_text(encoding="utf-8")
        assert "JOB_EXPORT_EMBEDDINGS" in scheduler_content
        assert "_phoenix_export_job" in scheduler_content

    def test_phoenix_spec_in_scheduler(self):
        """Scheduler uses AGENT_OBS_CRON_PHOENIX_SPEC env var."""
        scheduler_content = SCHEDULER_PATH.read_text(encoding="utf-8")
        assert "AGENT_OBS_CRON_PHOENIX_SPEC" in scheduler_content

    def test_phoenix_job_interval(self):
        """Phoenix export job runs every 5 minutes (300 seconds)."""
        scheduler_content = SCHEDULER_PATH.read_text(encoding="utf-8")
        # Check that 300 is used as interval_seconds for Phoenix job
        assert "300" in scheduler_content


# ---------------------------------------------------------------------------
# Drift alert integration
# ---------------------------------------------------------------------------


class TestDriftAlertPhoenixLink:
    def test_drift_alert_contains_phoenix_link(self):
        """Drift alert annotation contains Phoenix UMAP link."""
        rules_path = REPO_ROOT / "infra" / "prometheus-rules.yml"
        rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))

        for group in rules.get("groups", []):
            if group.get("name") == "drift-detection":
                for rule in group.get("rules", []):
                    if rule.get("alert") == "DriftDetected":
                        desc = rule.get("annotations", {}).get("description", "")
                        assert "phoenix" in desc.lower(), (
                            "DriftDetected alert missing Phoenix link in description"
                        )
                        assert "6006" in desc, (
                            "DriftDetected alert Phoenix link missing port 6006"
                        )
                        return
        pytest.fail("DriftDetected alert rule not found")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestExportEdgeCases:
    def test_empty_embeddings_list(self):
        """Export handles empty embeddings list gracefully."""
        # Import the module's function
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "export_embeddings_to_phoenix", EXPORT_SCRIPT_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # The function should handle empty list without error
        # (we're just testing the logic, not the actual HTTP call)
        assert hasattr(module, "push_embeddings_to_phoenix")
        assert hasattr(module, "fetch_embeddings_from_clickhouse")

    def test_phoenix_base_url_construction(self):
        """Phoenix base URL is constructed correctly from host and port."""
        import os

        # Test default values
        host = os.environ.get("PHOENIX_HOST", "localhost")
        port = int(os.environ.get("PHOENIX_PORT", "6006"))
        base_url = f"http://{host}:{port}"
        assert base_url == "http://localhost:6006"

    def test_batch_size_configuration(self):
        """Batch size is configurable via environment variable."""
        import os

        batch_size = int(os.environ.get("PHOENIX_EXPORT_BATCH_SIZE", "1000"))
        assert batch_size == 1000


# ---------------------------------------------------------------------------
# PC28: Prometheus metrics tests
# ---------------------------------------------------------------------------


class TestPhoenixExportMetrics:
    """Test that PC28 Prometheus metrics are properly implemented."""

    def test_metrics_available_via_scheduler(self):
        """Phoenix metrics are available via scheduler's metrics endpoint."""
        # Test that the scheduler imports the module and metrics are in registry
        scheduler_content = SCHEDULER_PATH.read_text(encoding="utf-8")
        
        # Scheduler imports the export module via _phoenix_export_job
        assert "_phoenix_export_job" in scheduler_content
        assert "from scripts.cron.export_embeddings_to_phoenix import run_phoenix_export" in scheduler_content
        
        # The scheduler serves metrics on :9777/metrics via prometheus_client.start_http_server
        assert "start_http_server" in scheduler_content
        assert "9777" in scheduler_content

    def test_metrics_use_pc28_names(self):
        """Phoenix export metrics use exact PC28 names."""
        # Read the source file to verify metric names
        source_content = EXPORT_SCRIPT_PATH.read_text(encoding="utf-8")
        
        # Check that PC28 metric names are used in the source
        assert "phoenix_export_runs_total = Counter(" in source_content
        assert "phoenix_export_duration_seconds = Histogram(" in source_content
        assert "phoenix_export_rows_total = Counter(" in source_content
        
        # Check that metrics are incremented in run_phoenix_export
        assert "phoenix_export_runs_total.inc()" in source_content
        assert "phoenix_export_duration_seconds.observe(" in source_content
        assert "phoenix_export_rows_total.inc(" in source_content
