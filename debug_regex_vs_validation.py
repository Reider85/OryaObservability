"""Debug regex vs validation in _inn_detector."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_regex_vs_validation():
    print("=== Debug Regex vs Validation ===")
    
    detector = PIIDetector()
    
    test_cases = [
        "1234567890",   # 10 digits, should be found by regex but invalid
        "111111111111", # 12 digits, should be found by regex but invalid
    ]
    
    for inn in test_cases:
        print(f"\nTesting: '{inn}'")
        
        # Test regex directly
        import re
        pattern = r'\b[0-9]{10}\b|\b[0-9]{12}\b'
        regex_matches = re.findall(pattern, inn)
        print(f"Regex matches: {regex_matches}")
        
        # Test validation
        is_valid = detector._validate_inn(inn)
        print(f"Validation: {is_valid}")
        
        # Test if _inn_detector finds it
        text = f"INN: {inn}"
        matches = detector._inn_detector(text)
        print(f"_inn_detector matches: {len(matches)}")
        
        # Debug step by step
        print("Step by step:")
        for match in re.finditer(pattern, text):
            inn_value = match.group()
            print(f"  Found regex: '{inn_value}'")
            valid_check = detector._validate_inn(inn_value)
            print(f"    Validation check: {valid_check}")
            if valid_check:
                print(f"    Would add to result")
            else:
                print(f"    Would NOT add to result (invalid)")

if __name__ == "__main__":
    debug_regex_vs_validation()