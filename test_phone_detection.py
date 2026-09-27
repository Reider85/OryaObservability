"""Test that phone detection still works after removing false positives."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def test_phone_detection():
    print("=== Test Phone Detection After Fix ===")
    
    detector = PIIDetector()
    
    # Test real phone numbers
    test_cases = [
        ("Call +7 (999) 123-45-67", ["+7 (999) 123-45-67"]),
        ("Phone: 89001234567", ["89001234567"]),
        ("Mobile: +79991234567", ["+79991234567"]),
        ("International: +1 (555) 123-4567", ["+1 (555) 123-4567"]),  # This should still work
    ]
    
    for text, expected in test_cases:
        matches = detector.detect(text)
        actual = [m.value for m in matches if m.entity_type == "phone"]
        print(f"Text: '{text}'")
        print(f"  Expected: {expected}")
        print(f"  Actual: {actual}")
        print(f"  [OK] Pass" if actual == expected else f"  [FAIL] Fail")
    
    # Test that the problematic fictional case is not detected
    neutral_text = """
    The quick brown fox jumps over the lazy dog. This is a sample text
    containing various words and phrases. It includes numbers like 12345
    and combinations that might look like data but are not actual PII.
    Email addresses like user@localhost or test@domain are examples.
    Phone numbers in fictional contexts like +1 (555) 123-4567 should
    not be detected if they are clearly fictional examples.
    """
    
    matches = detector.detect(neutral_text)
    phone_matches = [m for m in matches if m.entity_type == "phone"]
    print(f"\nNeutral text test:")
    print(f"  Phone matches: {len(phone_matches)}")
    print(f"  [OK] Pass" if len(phone_matches) == 0 else f"  [FAIL] Fail")

if __name__ == "__main__":
    test_phone_detection()