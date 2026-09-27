"""Test simple INN data."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def test_simple_inn():
    print("=== Testing Simple INN Data ===")
    
    detector = PIIDetector()
    
    simple_inns = [
        "0276453431",   # Original working one
        "1234567890",   # Simple individual
        "111111111111", # Simple entity
    ]
    
    for inn in simple_inns:
        print(f"\nINN: {inn}")
        
        # Test validation
        is_valid = detector._validate_inn(inn)
        print(f"Validation: {is_valid}")
        
        # Test detection
        text = f"INN: {inn}"
        matches = detector.detect(text)
        print(f"Detection: {len(matches)} matches")
        
        if matches:
            print(f"  Match: {matches[0].value}")

if __name__ == "__main__":
    test_simple_inn()