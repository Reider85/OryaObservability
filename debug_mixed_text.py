"""Debug mixed text scenario."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_mixed_text():
    print("=== Debug Mixed Text Scenario ===")
    
    detector = PIIDetector()
    
    text = """
    Dear John,
    
    Please contact our support team at support@company.com or call
    +7 (999) 123-45-67 for assistance. Your company INN is 5077466207712
    and your passport number is 1234 567890. For payment, we'll use
    card 4111111111111111 for billing.
    
    Best regards,
    Jane Smith
    """
    
    matches = detector.detect(text)
    
    print(f"Total matches: {len(matches)}")
    for match in matches:
        print(f"  {match.entity_type}: {match.value}")
    
    entity_types = {match.entity_type for match in matches}
    print(f"Entity types: {entity_types}")
    
    # Check what's missing
    expected_types = {"email", "phone", "inn", "passport", "payment"}
    print(f"Expected: {expected_types}")
    print(f"Missing: {expected_types - entity_types}")
    print(f"Extra: {entity_types - expected_types}")

if __name__ == "__main__":
    debug_mixed_text()