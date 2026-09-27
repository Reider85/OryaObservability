"""Debug PIIDetector INN method directly."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_inn_method():
    print("=== Debug PIIDetector._inn_method ===")
    
    detector = PIIDetector()
    
    # Test the corrected 12-digit INNs
    test_inns = [
        "507746620771",  # Corrected entity INN
        "770478106016",  # Corrected entity INN
    ]
    
    for inn in test_inns:
        text = f"INN: {inn}"
        print(f"\nTesting: '{text}'")
        
        # Call the method directly
        matches = detector._inn_detector(text)
        print(f"_inn_detector result: {len(matches)} matches")
        
        for match in matches:
            print(f"  Match: {match}")
        
        # Check validation
        is_valid = detector._validate_inn(inn)
        print(f"Validation: {is_valid}")

if __name__ == "__main__":
    debug_inn_method()