"""Debug INN validation issues."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_inn():
    print("=== Debug INN Validation ===")
    
    # Test the INN validation directly
    detector = PIIDetector()
    
    # Test cases from the test
    test_inns = [
        "0276453431",  # Valid individual (according to test)
        "7707087890",  # Valid individual (according to test)
        "5077466207712",  # Valid entity (according to test)
        "7704781060168",  # Valid entity (according to test)
    ]
    
    print("Testing INN validation directly:")
    for inn in test_inns:
        is_valid = detector._validate_inn(inn)
        print(f"INN {inn}: valid = {is_valid}")
        
        # Test detection
        text = f"INN: {inn}"
        matches = detector.detect(text)
        print(f"  Detection: {len(matches)} matches")
        if matches:
            print(f"    Match: {matches[0].value}")
    
    # Test with some known valid INNs from online sources
    known_valid_inns = [
        "7707087890",  # Known valid individual INN
        "5077466207712",  # Known valid entity INN
        "7704781060168",  # Another known valid entity INN
    ]
    
    print("\nTesting with known valid INNs:")
    for inn in known_valid_inns:
        is_valid = detector._validate_inn(inn)
        print(f"INN {inn}: valid = {is_valid}")

if __name__ == "__main__":
    debug_inn()