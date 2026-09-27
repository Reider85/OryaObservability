"""Test newly created valid INN with detector."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def test_new_valid_inn_with_detector():
    print("=== Testing New Valid INN with Detector ===")
    
    detector = PIIDetector()
    
    new_valid_inns = [
        "0276453431",   # Original
        "1111111117",   # New individual
        "1111111111136", # New entity
    ]
    
    for inn in new_valid_inns:
        print(f"\nINN: {inn}")
        
        # Test validation
        is_valid = detector._validate_inn(inn)
        print(f"Validation: {is_valid}")
        
        # Test regex
        import re
        pattern = r'\b[0-9]{10}\b|\b[0-9]{12}\b'
        regex_matches = re.findall(pattern, inn)
        print(f"Regex matches: {regex_matches}")
        
        # Test detection
        text = f"INN: {inn}"
        matches = detector.detect(text)
        print(f"Detection: {len(matches)} matches")
        
        # Test _inn_detector directly
        inn_matches = detector._inn_detector(text)
        print(f"_inn_detector: {len(inn_matches)} matches")
        
        if matches:
            print(f"  Match: {matches[0].value}")

if __name__ == "__main__":
    test_new_valid_inn_with_detector()