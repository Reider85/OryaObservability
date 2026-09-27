"""Debug script for PIIDetector issues."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_phone_detection():
    """Debug phone number detection."""
    print("=== Phone Detection Debug ===")
    detector = PIIDetector(enabled_detectors={"phone"})
    
    test_text = "Call +7 (999) 123-45-67 or 89001234567"
    print(f"Text: {test_text}")
    
    matches = detector.detect(test_text)
    print(f"Matches: {matches}")
    print(f"Phone values: {[m.value for m in matches]}")
    print()

def debug_inn_validation():
    """Debug INN validation."""
    print("=== INN Validation Debug ===")
    detector = PIIDetector(enabled_detectors={"inn"})
    
    # Test known valid INNs
    test_inns = ["0276453431", "7707087890", "5077466207712", "7704781060168"]
    
    for inn in test_inns:
        text = f"INN: {inn}"
        print(f"Text: {text}")
        
        matches = detector.detect(text)
        print(f"Matches: {matches}")
        print(f"INN detected: {len(matches) > 0}")
        
        # Check validation directly
        if hasattr(detector, '_validate_inn'):
            is_valid = detector._validate_inn(inn)
            print(f"Direct validation: {is_valid}")
        print()

def debug_passport_detection():
    """Debug passport detection."""
    print("=== Passport Detection Debug ===")
    detector = PIIDetector(enabled_detectors={"passport"})
    
    test_text = "Passport: 1234 567890"
    print(f"Text: {test_text}")
    
    matches = detector.detect(test_text)
    print(f"Matches: {matches}")
    print(f"Passport values: {[m.value for m in matches]}")
    print()

def debug_empty_env():
    """Debug empty environment variable."""
    print("=== Empty Environment Debug ===")
    
    # Test empty env var
    os.environ["AGENT_OBS_PII_DETECTORS"] = ""
    
    try:
        detector = PIIDetector()
        print(f"Enabled detectors: {detector.enabled_detectors}")
        print(f"Type: {type(detector.enabled_detectors)}")
        print(f"Length: {len(detector.enabled_detectors)}")
        print(f"Contents: {list(detector.enabled_detectors)}")
    finally:
        del os.environ["AGENT_OBS_PII_DETECTORS"]
    print()

if __name__ == "__main__":
    debug_empty_env()
    debug_phone_detection()
    debug_inn_validation()
    debug_passport_detection()