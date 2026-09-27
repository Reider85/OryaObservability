"""Debug recall rate issues."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_recall_rate():
    print("=== Debug Recall Rate Issues ===")
    
    detector = PIIDetector()
    
    # Problematic test cases from the dataset
    problem_cases = [
        ("Entity INN: 5077466207712", ["5077466207712"]),  # Old invalid INN
        ("Company: 7704781060168", ["7704781060168"]),  # Old invalid INN
        ("Document: 1234567890", ["1234567890"]),  # Passport without space
        ("Invalid INN: 1234567890", []),  # Invalid INN
        ("Short INN: 123456789", []),  # Too short
        ("Long INN: 123456789012345", []),  # Too long
        ("Invalid: 123 45678", []),  # Wrong length passport
        ("With letters: ABC123456", []),  # Contains letters passport
        ("Invalid: 4111111111111112", []),  # Invalid checksum card
        ("With letters: ABC123456789012", []),  # Contains letters card
    ]
    
    total_entities = 0
    detected_entities = 0
    
    for text, expected_entities in problem_cases:
        matches = detector.detect(text)
        actual_entities = [match.value for match in matches]
        
        total_entities += len(expected_entities)
        detected_in_case = 0
        
        for expected in expected_entities:
            if expected in actual_entities:
                detected_in_case += 1
        
        print(f"\nText: '{text}'")
        print(f"Expected: {expected_entities}")
        print(f"Actual: {actual_entities}")
        print(f"Detected: {detected_in_case}/{len(expected_entities)}")
        
        detected_entities += detected_in_case
    
    print(f"\nTotal entities: {total_entities}")
    print(f"Detected entities: {detected_entities}")
    recall_rate = detected_entities / total_entities if total_entities > 0 else 0
    print(f"Recall rate: {recall_rate:.3f}")

if __name__ == "__main__":
    debug_recall_rate()