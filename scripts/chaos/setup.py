#!/usr/bin/env python3
"""
Setup script for Chaos Engineering system.
"""
import os
import sys
import subprocess
import argparse
from pathlib import Path


def run_command(cmd, description=None):
    """Run a command and handle errors."""
    if description:
        print(f"\n{'='*60}")
        print(f"🔄 {description}")
        print(f"{'='*60}")
    
    print(f"📝 Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"❌ Error in {description}:")
        print(f"📤 stdout: {result.stdout}")
        print(f"📥 stderr: {result.stderr}")
        return False
    else:
        print(f"✅ {description} completed successfully")
        if result.stdout:
            print(f"📤 Output: {result.stdout}")
        return True


def install_dependencies():
    """Install chaos engineering dependencies."""
    print("🚀 Installing chaos engineering dependencies...")
    
    # Install core dependencies
    if not run_command("pip install -r scripts/chaos/requirements.txt", "Installing dependencies"):
        return False
    
    # Install additional packages for enhanced functionality
    packages = [
        "docker",
        "aiohttp",
        "pydantic",
        "structlog",
        "click",
        "rich"
    ]
    
    for package in packages:
        if not run_command(f"pip install {package}", f"Installing {package}"):
            print(f"⚠️  Failed to install {package}, but continuing...")
    
    return True


def setup_directories():
    """Create necessary directories."""
    print("📁 Setting up directories...")
    
    directories = [
        "scripts/chaos",
        "tests/test_chaos_engineering",
        "docs/chaos-reports",
        "config/chaos"
    ]
    
    for directory in directories:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        print(f"✅ Created directory: {path}")


def run_tests():
    """Run chaos engineering tests."""
    print("🧪 Running chaos engineering tests...")
    
    # Run unit tests
    if not run_command("python -m pytest tests/test_chaos_engineering -v", "Running unit tests"):
        return False
    
    # Run integration tests if HA stack is running
    if is_ha_stack_running():
        print("🌐 HA stack detected, running integration tests...")
        if not run_command("python -m pytest tests/test_chaos_engineering::TestIntegration -v", "Running integration tests"):
            print("⚠️  Integration tests failed, but continuing...")
    
    return True


def is_ha_stack_running():
    """Check if HA stack is running."""
    try:
        result = subprocess.run(
            "docker compose -f infra/docker-compose.ha.yml ps",
            shell=True,
            capture_output=True,
            text=True
        )
        return "running" in result.stdout.lower()
    except:
        return False


def generate_sample_config():
    """Generate sample configuration files."""
    print("⚙️  Generating sample configuration...")
    
    config_dir = Path("config/chaos")
    
    # Sample environment file
    env_content = """# Chaos Engineering Configuration
# Copy this file to .env and customize as needed

# General settings
CHAOS_ENGINEERING_ENABLED=true
CHAOS_OUTPUT_DIR=docs/chaos-reports
CHAOS_LOG_LEVEL=INFO

# SLO thresholds
CHAOS_SLO_AVAILABILITY_WARNING=99.5
CHAOS_SLO_AVAILABILITY_CRITICAL=99.0
CHAOS_SLO_LATENCY_WARNING=5000
CHAOS_SLO_LATENCY_CRITICAL=10000
CHAOS_SLO_ERROR_BUDGET_WARNING=20
CHAOS_SLO_ERROR_BUDGET_CRITICAL=50

# Chaos scenario settings
CHAOS_DEFAULT_DURATION_SECONDS=60
CHAOS_RECOVERY_TIMEOUT_SECONDS=300

# Notification settings (optional)
# CHAOS_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
# CHAOS_TELEGRAM_BOT_TOKEN=bot123456789:YOUR_TOKEN_HERE
# CHAOS_TELEGRAM_CHAT_ID=123456789

# Schedule settings
CHAOS_SCHEDULE_ENABLED=true
CHAOS_WEEKLY_CRON="0 2 * * 0"
CHAOS_RANDOM_ORDER=true
"""
    
    env_file = config_dir / ".env.example"
    with open(env_file, 'w') as f:
        f.write(env_content)
    
    print(f"✅ Generated sample config: {env_file}")


def validate_setup():
    """Validate the setup."""
    print("✅ Validating setup...")
    
    # Check required files
    required_files = [
        "scripts/chaos/__init__.py",
        "scripts/chaos/base.py",
        "scripts/chaos/chaos_scenarios.py",
        "scripts/chaos/slo_validator.py",
        "scripts/chaos/weekly_chaos.py",
        "scripts/chaos/config.py",
        "tests/test_chaos_engineering.py",
        "docs/chaos-runbook.md"
    ]
    
    missing_files = []
    for file_path in required_files:
        if not Path(file_path).exists():
            missing_files.append(file_path)
    
    if missing_files:
        print("❌ Missing required files:")
        for file_path in missing_files:
            print(f"   - {file_path}")
        return False
    
    # Check Python imports
    try:
        from scripts.chaos import WeeklyChaosEngine, ChaosConfig
        print("✅ Python imports successful")
    except ImportError as e:
        print(f"❌ Import error: {e}")
        return False
    
    return True


def main():
    """Main setup function."""
    parser = argparse.ArgumentParser(description="Setup Chaos Engineering system")
    parser.add_argument("--skip-tests", action="store_true", help="Skip running tests")
    parser.add_argument("--skip-deps", action="store_true", help="Skip installing dependencies")
    parser.add_argument("--validate-only", action="store_true", help="Only validate setup")
    parser.add_argument("--generate-config", action="store_true", help="Generate sample configuration")
    
    args = parser.parse_args()
    
    print("🎯 Chaos Engineering System Setup")
    print("=" * 50)
    
    if args.validate_only:
        if validate_setup():
            print("✅ Setup validation successful!")
        else:
            print("❌ Setup validation failed!")
            sys.exit(1)
        return
    
    # Setup directories
    setup_directories()
    
    # Generate sample config if requested
    if args.generate_config:
        generate_sample_config()
    
    # Install dependencies unless skipped
    if not args.skip_deps:
        if not install_dependencies():
            print("❌ Failed to install dependencies")
            sys.exit(1)
    
    # Run tests unless skipped
    if not args.skip_tests:
        if not run_tests():
            print("⚠️  Some tests failed, but continuing...")
    
    # Validate setup
    if validate_setup():
        print("\n🎉 Chaos Engineering system setup completed successfully!")
        print("\nNext steps:")
        print("1. Copy config/chaos/.env.example to .env and customize")
        print("2. Review docs/chaos-runbook.md for operational guidance")
        print("3. Run tests: python -m scripts.chaos.weekly_chaos")
        print("4. Schedule tests: add cron job for weekly_chaos.py")
    else:
        print("\n❌ Setup validation failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()