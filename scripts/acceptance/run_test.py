#!/usr/bin/env python3
"""
Individual acceptance test runner
Run specific criteria tests individually
"""

import sys
import importlib.util
from pathlib import Path

def run_test(test_name: str):
    """Run a specific test"""
    harness_path = Path("C:/projects/OryaObservability/scripts/acceptance/pc37_harness.py")
    
    spec = importlib.util.spec_from_file_location("pc37_harness", harness_path)
    harness_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness_module)
    
    harness = harness_module.CriticalAcceptanceHarness()
    
    # Map test names to methods
    test_methods = {
        "c1": harness.measure_c1_slo_alerting,
        "c2": harness.measure_c2_mttr_incidents,
        "c3": harness.measure_c3_pii_leakage,
        "c4": harness.measure_c4_drift_detection,
        "c5": harness.measure_c5_gdpr_data_map,
        "c6": harness.measure_c6_latency_guardrail,
        "c7": harness.measure_c7_pii_recall,
        "c8": harness.measure_c8_injection_f1,
        "c9": harness.measure_c9_latency_eval,
        "c10": harness.measure_c10_latency_eval_annotation,
        "c11": harness.measure_c11_hot_query_latency,
        "c12": harness.measure_c12_storage_cost,
        "c13": harness.measure_c13_overhead_reduction,
        "c14": harness.measure_c14_ideality,
    }
    
    if test_name.lower() in test_methods:
        print(f"Running {test_name.upper()} test...")
        result = test_methods[test_name.lower()]()
        print(f"\nResults for {test_name.upper()}:")
        print(f"Target: {result['target']}")
        print(f"Measured: {result['measured']:.3f}")
        print(f"Method: {result['method']}")
        print(f"Verdict: {result['verdict']}")
        print(f"Details: {result['details']}")
    else:
        print(f"Unknown test: {test_name}")
        print("Available tests: " + ", ".join(test_methods.keys()))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_test(sys.argv[1])
    else:
        print("Usage: python run_test.py <test_name>")
        print("Available tests: c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14")