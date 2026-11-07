"""
Weekly chaos engineering game-day script.
"""
import argparse
import asyncio
import json
import logging
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .base import ChaosEngine, ChaosScenario, ChaosStatus
from .chaos_scenarios import create_target_scenarios
from .slo_validator import ChaosSLOValidator, create_default_slo_thresholds


class ChaosReporter:
    """Handles reporting of chaos engineering results."""
    
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def generate_report(self, results: List, summary: Dict) -> str:
        """Generate chaos engineering report."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = self.output_dir / f"chaos_report_{timestamp}.json"
        
        report = {
            "timestamp": datetime.now().isoformat(),
            "summary": summary,
            "results": [
                {
                    "scenario_name": r.scenario_name,
                    "status": r.status.value,
                    "start_time": r.start_time.isoformat(),
                    "end_time": r.end_time.isoformat() if r.end_time else None,
                    "duration_seconds": (r.end_time - r.start_time).total_seconds() if r.end_time else None,
                    "services_affected": r.services_affected,
                    "slo_violations": r.slo_violations,
                    "recovery_time_seconds": r.recovery_time_seconds,
                    "error_message": r.error_message,
                    "metadata": r.metadata
                }
                for r in results
            ]
        }
        
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        
        logging.info(f"Chaos report generated: {report_file}")
        return str(report_file)
    
    def generate_summary_report(self, results: List, summary: Dict) -> str:
        """Generate a human-readable summary report."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary_file = self.output_dir / f"chaos_summary_{timestamp}.md"
        
        with open(summary_file, 'w') as f:
            f.write("# Weekly Chaos Engineering Report\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
            f.write("## Summary\n\n")
            f.write(f"- **Total Scenarios:** {summary['total_scenarios']}\n")
            f.write(f"- **Successful:** {summary['successful']}\n")
            f.write(f"- **Failed:** {summary['failed']}\n")
            f.write(f"- **Success Rate:** {summary['success_rate']:.1%}\n")
            f.write(f"- **Average Recovery Time:** {summary['average_recovery_time']:.1f}s\n\n")
            
            f.write("## Detailed Results\n\n")
            
            for result in results:
                f.write(f"### {result.scenario_name}\n\n")
                f.write(f"- **Status:** {result.status.value}\n")
                f.write(f"- **Start Time:** {result.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                if result.end_time:
                    f.write(f"- **End Time:** {result.end_time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    duration = (result.end_time - result.start_time).total_seconds()
                    f.write(f"- **Duration:** {duration:.1f}s\n")
                if result.recovery_time_seconds:
                    f.write(f"- **Recovery Time:** {result.recovery_time_seconds:.1f}s\n")
                if result.services_affected:
                    f.write(f"- **Services Affected:** {', '.join(result.services_affected)}\n")
                if result.slo_violations:
                    f.write(f"- **SLO Violations:** {result.slo_violations}\n")
                if result.error_message:
                    f.write(f"- **Error:** {result.error_message}\n")
                f.write("\n")
        
        logging.info(f"Summary report generated: {summary_file}")
        return str(summary_file)


class ChaosNotifier:
    """Handles notifications for chaos engineering results."""
    
    def __init__(self, webhook_url: Optional[str] = None):
        self.webhook_url = webhook_url
    
    async def notify_success(self, summary: Dict, report_files: List[str]) -> None:
        """Notify about successful chaos test."""
        message = {
            "text": "🎉 Weekly Chaos Engineering Test - SUCCESS",
            "attachments": [
                {
                    "color": "good",
                    "fields": [
                        {"title": "Summary", "value": f"Success Rate: {summary['success_rate']:.1%}", "short": True},
                        {"title": "Total Scenarios", "value": str(summary['total_scenarios']), "short": True},
                        {"title": "Failed", "value": str(summary['failed']), "short": True},
                        {"title": "Average Recovery Time", "value": f"{summary['average_recovery_time']:.1f}s", "short": True}
                    ]
                }
            ]
        }
        
        await self._send_webhook(message)
    
    async def notify_failure(self, summary: Dict, report_files: List[str]) -> None:
        """Notify about failed chaos test."""
        message = {
            "text": "⚠️ Weekly Chaos Engineering Test - FAILED",
            "attachments": [
                {
                    "color": "danger",
                    "fields": [
                        {"title": "Summary", "value": f"Success Rate: {summary['success_rate']:.1%}", "short": True},
                        {"title": "Total Scenarios", "value": str(summary['total_scenarios']), "short": True},
                        {"title": "Failed", "value": str(summary['failed']), "short": True},
                        {"title": "Average Recovery Time", "value": f"{summary['average_recovery_time']:.1f}s", "short": True}
                    ]
                }
            ]
        }
        
        await self._send_webhook(message)
    
    async def _send_webhook(self, message: Dict) -> None:
        """Send webhook notification."""
        if not self.webhook_url:
            return
        
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(self.webhook_url, json=message) as response:
                    if response.status == 200:
                        logging.info("Webhook notification sent successfully")
                    else:
                        logging.error(f"Failed to send webhook: {response.status}")
        except Exception as e:
            logging.error(f"Error sending webhook: {e}")


class WeeklyChaosEngine:
    """Main weekly chaos engineering orchestrator."""
    
    def __init__(self, output_dir: Path, webhook_url: Optional[str] = None):
        self.output_dir = output_dir
        self.reporter = ChaosReporter(output_dir)
        self.notifier = ChaosNotifier(webhook_url)
        self.scenarios = create_target_scenarios()
        self.slo_validator = ChaosSLOValidator(create_default_slo_thresholds())
        self.chaos_engine = ChaosEngine(self.scenarios, self.slo_validator)
    
    async def run_chaos_session(self, scenario_name: Optional[str] = None, 
                               random_order: bool = False) -> Dict:
        """Run a chaos engineering session."""
        logging.info("Starting weekly chaos engineering session")
        
        # Filter scenarios if specific scenario is requested
        if scenario_name:
            scenarios = [s for s in self.scenarios if s.name == scenario_name]
            if not scenarios:
                raise ValueError(f"Scenario {scenario_name} not found")
        else:
            scenarios = self.scenarios
        
        # Shuffle scenarios if random order is requested
        if random_order:
            random.shuffle(scenarios)
            self.chaos_engine.scenarios = scenarios
        
        # Run scenarios
        results = []
        for scenario in scenarios:
            logging.info(f"Running scenario: {scenario.name}")
            result = await self.chaos_engine.run_scenario(scenario.name)
            results.append(result)
            
            # Check if we need to abort due to failures
            if result.status == ChaosStatus.FAILED:
                logging.error(f"Scenario {scenario.name} failed, aborting session")
                break
        
        # Generate summary
        summary = self.chaos_engine.get_execution_summary()
        
        # Generate reports
        report_file = self.reporter.generate_report(results, summary)
        summary_file = self.reporter.generate_summary_report(results, summary)
        
        # Send notifications
        if summary['success_rate'] >= 0.8:  # 80% success rate
            await self.notifier.notify_success(summary, [report_file, summary_file])
        else:
            await self.notifier.notify_failure(summary, [report_file, summary_file])
        
        logging.info(f"Chaos session completed. Reports: {report_file}, {summary_file}")
        
        return {
            "summary": summary,
            "results": results,
            "report_file": report_file,
            "summary_file": summary_file
        }
    
    async def run_single_scenario(self, scenario_name: str) -> Dict:
        """Run a single chaos scenario."""
        logging.info(f"Running single scenario: {scenario_name}")
        
        result = await self.chaos_engine.run_scenario(scenario_name)
        summary = self.chaos_engine.get_execution_summary()
        
        # Generate reports
        report_file = self.reporter.generate_report([result], summary)
        summary_file = self.reporter.generate_summary_report([result], summary)
        
        # Send notifications
        if result.status == ChaosStatus.COMPLETED:
            await self.notifier.notify_success(summary, [report_file, summary_file])
        else:
            await self.notifier.notify_failure(summary, [report_file, summary_file])
        
        logging.info(f"Single scenario completed. Reports: {report_file}, {summary_file}")
        
        return {
            "summary": summary,
            "result": result,
            "report_file": report_file,
            "summary_file": summary_file
        }


def main():
    """Main entry point for weekly chaos script."""
    parser = argparse.ArgumentParser(description="Weekly Chaos Engineering Game-Day")
    parser.add_argument("--scenario", type=str, help="Run specific scenario")
    parser.add_argument("--random", action="store_true", help="Run scenarios in random order")
    parser.add_argument("--output-dir", type=str, default="docs/chaos-reports", 
                       help="Output directory for reports")
    parser.add_argument("--webhook-url", type=str, help="Webhook URL for notifications")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    
    args = parser.parse_args()
    
    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Initialize chaos engine
    output_dir = Path(args.output_dir)
    webhook_url = args.webhook_url
    
    try:
        chaos_engine = WeeklyChaosEngine(output_dir, webhook_url)
        
        # Run chaos session
        if args.scenario:
            asyncio.run(chaos_engine.run_single_scenario(args.scenario))
        else:
            asyncio.run(chaos_engine.run_chaos_session(random_order=args.random))
            
    except Exception as e:
        logging.error(f"Chaos engineering session failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()