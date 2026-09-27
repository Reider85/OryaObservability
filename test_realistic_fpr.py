"""Test false positive rate in more realistic scenarios."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def test_false_positive_rate():
    print("=== Test False Positive Rate ===")
    
    detector = PIIDetector()
    
    # More realistic neutral text (not specifically designed to trick the detector)
    realistic_neutral_texts = [
        "The meeting is scheduled for next Tuesday at 3 PM.",
        "The project deadline is March 15, 2024.",
        "Our office is located at 123 Main Street, Anytown, CA 90210.",
        "The invoice number is INV-2024-001 for $1,234.56.",
        "Contact us at support@company.com for help.",
        "Call our support line at +1 (555) 123-4567.",
    ]
    
    total_words = 0
    total_matches = 0
    
    for text in realistic_neutral_texts:
        matches = detector.detect(text)
        total_words += len(text.split())
        total_matches += len(matches)
    
    false_positive_rate = total_matches / total_words if total_words > 0 else 0
    
    print(f"Total words: {total_words}")
    print(f"Total matches: {total_matches}")
    print(f"False positive rate: {false_positive_rate:.3f}")
    print(f"Requirement: < 0.01")
    print(f"[OK] Pass" if false_positive_rate < 0.01 else f"[FAIL] Fail")
    
    # Show what was detected
    print("\nDetected entities:")
    for text in realistic_neutral_texts:
        matches = detector.detect(text)
        if matches:
            print(f"  '{text}' -> {[m.value for m in matches]}")

if __name__ == "__main__":
    test_false_positive_rate()