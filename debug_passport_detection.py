"""Debug passport detection."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_passport_detection():
    print("=== Debug Passport Detection ===")
    
    detector = PIIDetector()
    
    test_passports = [
        "1234 567890",  # Series + number format
        "987654 321098",  # Another format
        "1234567890",  # Without space
    ]
    
    for passport in test_passports:
        print(f"\nTesting passport: '{passport}'")
        
        # Test regex directly
        import re
        pattern = r'\b[0-9]{4}\s?[0-9]{6}\b'
        regex_matches = re.findall(pattern, passport)
        print(f"Regex matches: {regex_matches}")
        
        # Test detection
        text = f"Passport: {passport}"
        matches = detector.detect(text)
        print(f"Detection: {len(matches)} matches")
        
        # Test _passport_detector directly
        passport_matches = detector._passport_detector(text)
        print(f"_passport_detector: {len(passport_matches)} matches")
        
        if passport_matches:
            print(f"  Match: {passport_matches[0].value}")

if __name__ == "__main__":
    debug_passport_detection()