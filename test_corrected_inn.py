"""Test corrected INN data."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def test_corrected_inn():
    print("=== Testing Corrected INN Data ===")
    
    detector = PIIDetector()
    
    # Corrected test data
    valid_individual_inns = [
        "0276453431",  # Valid individual INN
        "7707087890",  # Valid individual INN
    ]
    
    valid_entity_inns = [
        "507746620771",  # Valid entity INN
        "770478106016",  # Valid entity INN
    ]
    
    print("=== Validation Test ===")
    for inn in valid_individual_inns + valid_entity_inns:
        is_valid = detector._validate_inn(inn)
        print(f"INN {inn}: valid = {is_valid}")
    
    print("\n=== Detection Test ===")
    for inn in valid_individual_inns + valid_entity_inns:
        text = f"INN: {inn}"
        matches = detector.detect(text)
        print(f"Text: '{text}' -> {len(matches)} matches")
        if matches:
            print(f"  Match: {matches[0].value}")

if __name__ == "__main__":
    test_corrected_inn()