"""Test new INN data with detector."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def test_new_inn_data():
    print("=== Testing New INN Data ===")
    
    detector = PIIDetector()
    
    new_inns = [
        "0276453431",   # Individual
        "772855394477", # Entity 1
        "770259728596", # Entity 2
    ]
    
    for inn in new_inns:
        print(f"\nTesting INN: {inn}")
        
        # Test detection
        text = f"INN: {inn}"
        matches = detector.detect(text)
        print(f"Detection: {len(matches)} matches")
        
        # Test validation
        is_valid = detector._validate_inn(inn)
        print(f"Validation: {is_valid}")
        
        # Test regex directly
        import re
        pattern = r'\b[0-9]{10}\b|\b[0-9]{12}\b'
        regex_matches = re.findall(pattern, text)
        print(f"Regex: {regex_matches}")

if __name__ == "__main__":
    test_new_inn_data()