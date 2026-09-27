"""Analyze which phone numbers are not being detected."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def analyze_missed_phone_numbers():
    print("=== Analyze Missed Phone Numbers ===")
    
    detector = PIIDetector()
    
    # Test dataset with known PII entities
    test_cases = [
        ("Call +7 (999) 123-45-67", ["+7 (999) 123-45-67"]),
        ("Phone: 89001234567", ["89001234567"]),
        ("Mobile: +79991234567", ["+79991234567"]),
        ("International: +1 555-123-4567", ["+1 555-123-4567"]),
        ("UK: +44 20 7946 0958", ["+44 20 7946 0958"]),
        ("No spaces: +79991234567", ["+79991234567"]),
        ("With dots: +7.999.123.45.67", ["+7.999.123.45.67"]),
        ("With dashes: +7-999-123-45-67", ["+7-999-123-45-67"]),
        ("In text: My phone is +7 (999) 123-45-67", ["+7 (999) 123-45-67"]),
        ("Multiple: +7 (999) 123-45-67 and 89001234567", ["+7 (999) 123-45-67", "89001234567"]),
        ("Russian: 89001234567, +7 (999) 123-45-67", ["89001234567", "+7 (999) 123-45-67"]),
        ("International: +1 (555) 123-4567", ["+1 (555) 123-4567"]),  # This one is missed
        ("US: +1 555-123-4567", ["+1 555-123-4567"]),
        ("Canada: +1 (416) 555-1234", ["+1 (416) 555-1234"]),  # This one is missed
        ("Europe: +49 30 1234567", ["+49 30 1234567"]),
        ("Asia: +81 3-1234-5678", ["+81 3-1234-5678"]),
        ("Australia: +61 2 1234 5678", ["+61 2 1234 5678"]),
        ("In parentheses: Phone: (+7 999) 123-45-67", ["(+7 999) 123-45-67"]),
        ("In quotes: '+7 (999) 123-45-67' is my number", ["+7 (999) 123-45-67"]),
        ("Context: Call +7 (999) 123-45-67 or +1 555-123-4567", ["+7 (999) 123-45-67", "+1 555-123-4567"]),
    ]
    
    missed_count = 0
    for text, expected_phones in test_cases:
        matches = detector.detect(text)
        actual_phones = [match.value for match in matches]
        
        missed = [phone for phone in expected_phones if phone not in actual_phones]
        if missed:
            missed_count += len(missed)
            print(f"Text: '{text}'")
            print(f"  Expected: {expected_phones}")
            print(f"  Actual: {actual_phones}")
            print(f"  Missed: {missed}")
    
    print(f"\nTotal missed: {missed_count}")
    print("The main issue is +1 (XXX) XXX-XXXX pattern causes false positives")

if __name__ == "__main__":
    analyze_missed_phone_numbers()