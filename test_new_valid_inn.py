"""Test newly created valid INN."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def test_new_valid_inn():
    print("=== Testing New Valid INN ===")
    
    detector = PIIDetector()
    
    new_valid_inns = [
        "0276453431",   # Original
        "1234567890",   # New
        "1111111111",   # New
        "111111111111", # New entity
    ]
    
    for inn in new_valid_inns:
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
    test_new_valid_inn()