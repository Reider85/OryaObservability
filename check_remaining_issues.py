"""Check remaining recall issues."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def check_remaining_issues():
    print("=== Check Remaining Recall Issues ===")
    
    detector = PIIDetector()
    
    # Still problematic cases
    problem_cases = [
        ("Context: For more info, email info@company.com or call support.", ["info@company.com", "support"]),
        ("With dots: +7.999.123.45.67", ["+7.999.123.45.67"]),
        ("With dashes: +7-999-123-45-67", ["+7-999-123-45-67"]),
        ("Europe: +49 30 1234567", ["+49 30 1234567"]),
        ("Asia: +81 3-1234-5678", ["+81 3-1234-5678"]),
        ("Australia: +61 2 1234 5678", ["+61 2 1234 5678"]),
        ("In parentheses: Phone: (+7 999) 123-45-67", ["(+7 999) 123-45-67"]),
        ("Entity: Company INN 5077466207712", ["5077466207712"]),  # Still old invalid
        ("In document: Entity INN: 5077466207712", ["5077466207712"]),  # Still old invalid
        ("In form: Enter INN: 7704781060168", ["7704781060168"]),  # Still old invalid
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
    check_remaining_issues()