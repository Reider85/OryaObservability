"""Debug false positives in neutral text."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_false_positives():
    print("=== Debug False Positives ===")
    
    detector = PIIDetector()
    
    # Neutral text from literature (should have no PII)
    neutral_text = """
    The quick brown fox jumps over the lazy dog. This is a sample text
    containing various words and phrases. It includes numbers like 12345
    and combinations that might look like data but are not actual PII.
    Email addresses like user@localhost or test@domain are examples.
    Phone numbers in fictional contexts like +1 (555) 123-4567 should
    not be detected if they are clearly fictional examples.
    """
    
    matches = detector.detect(neutral_text)
    
    print(f"Total matches: {len(matches)}")
    print(f"Total words: {len(neutral_text.split())}")
    print(f"False positive rate: {len(matches) / len(neutral_text.split()):.3f}")
    
    for match in matches:
        print(f"  {match.entity_type}: '{match.value}' at position {match.span_start}-{match.span_end}")
    
    # Check what's causing false positives
    if matches:
        print("\nFalse positive analysis:")
        for match in matches:
            context_start = max(0, match.span_start - 20)
            context_end = min(len(neutral_text), match.span_end + 20)
            context = neutral_text[context_start:context_end]
            print(f"  '{match.value}' in context: ...{context}...")

if __name__ == "__main__":
    debug_false_positives()