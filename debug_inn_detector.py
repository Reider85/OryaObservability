"""Debug _inn_detector method directly."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_inn_detector():
    print("=== Debug _inn_detector Method ===")
    
    detector = PIIDetector()
    
    test_cases = [
        "INN: 0276453431",   # Should work
        "INN: 1234567890",   # Should be detected but invalid
        "INN: 111111111111", # Should be detected but invalid
    ]
    
    for text in test_cases:
        print(f"\nText: '{text}'")
        
        # Call method directly
        matches = detector._inn_detector(text)
        print(f"_inn_detector result: {len(matches)} matches")
        
        for match in matches:
            print(f"  Match: {match}")
        
        # Check if validation is the issue
        if matches:
            for match in matches:
                inn_value = match.value
                is_valid = detector._validate_inn(inn_value)
                print(f"  Validation for '{inn_value}': {is_valid}")

if __name__ == "__main__":
    debug_inn_detector()