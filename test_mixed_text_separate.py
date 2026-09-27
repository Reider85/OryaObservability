"""Test mixed text scenario separately."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def test_mixed_text():
    """Test detection in realistic mixed text."""
    detector = PIIDetector()
    
    text = """
    Dear John,
    
    Please contact our support team at support@company.com or call
    +7 (999) 123-45-67 for assistance. Your company INN is 111111111130
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
    
    # Check all entity types
    expected_types = {"email", "phone", "inn", "passport", "payment"}
    print(f"Expected: {expected_types}")
    print(f"Missing: {expected_types - entity_types}")
    print(f"Extra: {entity_types - expected_types}")
    
    # Should detect 5 entities
    assert len(matches) == 5
    
    # Check all entity types
    assert entity_types == {"email", "phone", "inn", "passport", "payment"}
    
    # Check specific values
    email_matches = [m for m in matches if m.entity_type == "email"]
    assert len(email_matches) == 1
    assert email_matches[0].value == "support@company.com"
    
    phone_matches = [m for m in matches if m.entity_type == "phone"]
    assert len(phone_matches) == 1
    assert phone_matches[0].value == "+7 (999) 123-45-67"
    
    print("✓ Mixed text test passed!")

if __name__ == "__main__":
    test_mixed_text()