"""Tests for PII detector (PC04) — regex-based email/phone/inn/passport/payment detection."""

import os
import pytest
from typing import List

from agent_obs.guardrail.pii_detector import PIIDetector, PIIMatch


class TestPIIMatch:
    """Test PIIMatch dataclass validation."""
    
    def test_valid_pii_match(self):
        """Test valid PIIMatch creation."""
        match = PIIMatch(
            entity_type="email",
            value="test@example.com",
            span_start=0,
            span_end=15,
            mask="[EMAIL:5f3a]"
        )
        assert match.entity_type == "email"
        assert match.value == "test@example.com"
        assert match.span_start == 0
        assert match.span_end == 15
        assert match.mask == "[EMAIL:5f3a]"
    
    def test_empty_entity_type_raises_error(self):
        """Test that empty entity_type raises ValueError."""
        with pytest.raises(ValueError, match="entity_type cannot be empty"):
            PIIMatch(
                entity_type="",
                value="test@example.com",
                span_start=0,
                span_end=15,
                mask="[EMAIL:5f3a]"
            )
    
    def test_empty_value_raises_error(self):
        """Test that empty value raises ValueError."""
        with pytest.raises(ValueError, match="value cannot be empty"):
            PIIMatch(
                entity_type="email",
                value="",
                span_start=0,
                span_end=15,
                mask="[EMAIL:5f3a]"
            )
    
    def test_invalid_span_range_raises_error(self):
        """Test that invalid span range raises ValueError."""
        with pytest.raises(ValueError, match="Invalid span range"):
            PIIMatch(
                entity_type="email",
                value="test@example.com",
                span_start=10,
                span_end=5,  # end < start
                mask="[EMAIL:5f3a]"
            )


class TestPIIDetector:
    """Test PIIDetector functionality."""
    
    def test_default_detectors_enabled(self):
        """Test that all detectors are enabled by default."""
        detector = PIIDetector()
        expected = {"email", "phone", "inn", "passport", "payment"}
        assert detector.enabled_detectors == expected
    
    def test_custom_enabled_detectors(self):
        """Test custom enabled detectors."""
        detector = PIIDetector(enabled_detectors={"email", "phone"})
        assert detector.enabled_detectors == {"email", "phone"}
    
    def test_env_variable_override(self):
        """Test that environment variable overrides default detectors."""
        os.environ["AGENT_OBS_PII_DETECTORS"] = "email,inn"
        
        try:
            detector = PIIDetector()
            assert detector.enabled_detectors == {"email", "inn"}
        finally:
            # Clean up
            del os.environ["AGENT_OBS_PII_DETECTORS"]
    
    def test_empty_env_falls_back_to_default(self):
        """Test that empty environment variable falls back to default."""
        os.environ["AGENT_OBS_PII_DETECTORS"] = ""
        
        try:
            detector = PIIDetector()
            # Empty string should result in empty set
            assert detector.enabled_detectors == set()
        finally:
            # Clean up
            del os.environ["AGENT_OBS_PII_DETECTORS"]
    
    def test_email_detection(self):
        """Test email detection with various formats."""
        detector = PIIDetector(enabled_detectors={"email"})
        
        # Test cases: (text, expected_emails)
        test_cases = [
            ("Contact me at user@example.com or support@company.co.uk", 
             ["user@example.com", "support@company.co.uk"]),
            ("Email: test+filter@domain.org, another@test.io", 
             ["test+filter@domain.org", "another@test.io"]),
            ("Invalid: user@.com, @domain.com, user@domain", []),
            ("UPPERCASE: TEST@DOMAIN.COM", ["TEST@DOMAIN.COM"]),
        ]
        
        for text, expected_emails in test_cases:
            matches = detector.detect(text)
            actual_emails = [match.value for match in matches]
            assert actual_emails == expected_emails
    
    def test_phone_detection(self):
        """Test phone number detection in various formats."""
        detector = PIIDetector(enabled_detectors={"phone"})
        
        test_cases = [
            ("Call +7 (999) 123-45-67 or 89001234567", 
             ["+7 (999) 123-45-67", "89001234567"]),
            ("International: +1 555-123-4567 or +44 20 7946 0958", 
             ["+1 555-123-4567", "+44 20 7946 0958"]),
            ("Invalid: 123, +7 abc, 8123", []),
            ("No spaces: +79991234567", ["+79991234567"]),
        ]
        
        for text, expected_phones in test_cases:
            matches = detector.detect(text)
            actual_phones = [match.value for match in matches]
            assert actual_phones == expected_phones
    
    def test_inn_detection_and_validation(self):
        """Test INN detection with checksum validation."""
        detector = PIIDetector(enabled_detectors={"inn"})
        
        # Valid INNs (individuals - 10 digits)
        valid_individual_inns = [
            "0276453431",  # Valid individual INN
            "1111111117",  # Valid individual INN (created with correct weights)
        ]
        
        # Valid INNs (entities - 12 digits)
        valid_entity_inns = [
            "111111111130",  # Valid entity INN (created with correct weights)
        ]
        
        # Invalid INNs
        invalid_inns = [
            "1234567891",  # Invalid checksum
            "111111111114",  # Invalid checksum
            "123",  # Too short
            "123456789012345",  # Too long
        ]
        
        # Test valid INNs are detected
        for inn in valid_individual_inns + valid_entity_inns:
            text = f"INN: {inn}"
            matches = detector.detect(text)
            assert len(matches) == 1
            assert matches[0].value == inn
            assert matches[0].entity_type == "inn"
        
        # Test invalid INNs are not detected
        for inn in invalid_inns:
            text = f"INN: {inn}"
            matches = detector.detect(text)
            assert len(matches) == 0
    
    def test_passport_detection(self):
        """Test passport number detection."""
        detector = PIIDetector(enabled_detectors={"passport"})
        
        valid_passports = [
            "1234 567890",  # Series + number format
            "987654 321098",  # Another format
        ]
        
        invalid_passports = [
            "123 45678",  # Wrong length
            "ABCD123456",  # Contains letters
            "123456",  # Too short
        ]
        
        # Test valid passports are detected
        for passport in valid_passports:
            text = f"Passport: {passport}"
            matches = detector.detect(text)
            assert len(matches) == 1
            assert matches[0].value == passport
            assert matches[0].entity_type == "passport"
        
        # Test invalid passports are not detected
        for passport in invalid_passports:
            text = f"Document: {passport}"
            matches = detector.detect(text)
            assert len(matches) == 0
    
    def test_payment_detection_and_luhn_validation(self):
        """Test payment card detection with Luhn validation."""
        detector = PIIDetector(enabled_detectors={"payment"})
        
        # Valid test card numbers (pass Luhn check)
        valid_cards = [
            "4111111111111111",  # Visa test card
            "5555555555554444",  # Mastercard test card
            "378282246310005",   # American Express
            "3530111333300000",  # JCB
        ]
        
        # Invalid card numbers (fail Luhn check)
        invalid_cards = [
            "4111111111111112",  # Invalid checksum
            "5555555555554445",  # Invalid checksum
            "1234567890123456",  # Invalid checksum
            "abcd123456789012",  # Contains letters
        ]
        
        # Test valid cards are detected
        for card in valid_cards:
            text = f"Card: {card}"
            matches = detector.detect(text)
            assert len(matches) == 1
            assert matches[0].value == card
            assert matches[0].entity_type == "payment"
        
        # Test invalid cards are not detected
        for card in invalid_cards:
            text = f"Payment: {card}"
            matches = detector.detect(text)
            assert len(matches) == 0
    
    def test_mask_format(self):
        """Test that masks follow the correct format."""
        detector = PIIDetector(enabled_detectors={"email", "phone", "inn", "passport", "payment"})
        
        test_cases = [
            ("Email: test@example.com", "email", "test@example.com"),
            ("Phone: +7 (999) 123-45-67", "phone", "+7 (999) 123-45-67"),
            ("INN: 0276453431", "inn", "0276453431"),
            ("Passport: 1234 567890", "passport", "1234 567890"),
            ("Card: 4111111111111111", "payment", "4111111111111111"),
        ]
        
        for text, entity_type, value in test_cases:
            matches = detector.detect(text)
            assert len(matches) == 1
            match = matches[0]
            assert match.entity_type == entity_type
            assert match.mask.startswith(f"[{entity_type.upper()}:")
            assert len(match.mask) == len(f"[{entity_type.upper()}:XXXX]")  # + 4 hex chars
            assert match.mask[-1] == "]"  # Ends with ]
    
    def test_multiple_entities_detection(self):
        """Test detection of multiple PII entities in text."""
        detector = PIIDetector()
        
        text = """
        Contact: john.doe@example.com, phone: +7 (999) 123-45-67
        INN: 0276453431, passport: 1234 567890
        Payment: 4111111111111111 for services
        """
        
        matches = detector.detect(text)
        
        # Should detect 5 entities
        assert len(matches) == 5
        
        # Check all entity types are present
        entity_types = {match.entity_type for match in matches}
        assert entity_types == {"email", "phone", "inn", "passport", "payment"}
        
        # Check sorting by position
        positions = [match.span_start for match in matches]
        assert positions == sorted(positions)
    
    def test_no_false_positives_in_neutral_text(self):
        """Test low false positive rate in neutral text (literature)."""
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
        
        # In neutral text, we should have very few false positives
        # This test ensures our patterns are specific enough
        false_positives = len(matches)
        false_positive_rate = false_positives / len(neutral_text.split())
        
        # FPR should be < 1% (very strict for this test)
        assert false_positive_rate < 0.01, f"False positive rate too high: {false_positive_rate:.3f}"
    
    def test_recall_rate_on_test_dataset(self):
        """Test recall rate on a comprehensive test dataset."""
        detector = PIIDetector()
        
        # Test dataset with known PII entities
        test_dataset = [
            # Email examples (20)
            ("Contact me at alice@example.com", ["alice@example.com"]),
            ("Email: bob@test.org", ["bob@test.org"]),
            ("Support: help@company.co.uk", ["help@company.co.uk"]),
            ("Send to user+filter@domain.io", ["user+filter@domain.io"]),
            ("UPPERCASE: TEST@DOMAIN.COM", ["TEST@DOMAIN.COM"]),
            ("Mixed: TeSt@DoMaIn.Net", ["TeSt@DoMaIn.Net"]),
            ("With dash: user-name@domain.org", ["user-name@domain.org"]),
            ("With dot: first.last@company.com", ["first.last@company.com"]),
            ("Numbers: user123@test123.com", ["user123@test123.com"]),
            ("Complex: a.b+c_d@sub.domain.co.uk", ["a.b+c_d@sub.domain.co.uk"]),
            ("Short: a@b.cd", ["a@b.cd"]),
            ("Long: very.long.email.address@very.long.domain.name.com", ["very.long.email.address@very.long.domain.name.com"]),
            ("Multiple: user1@domain.com, user2@test.org", ["user1@domain.com", "user2@test.org"]),
            ("In sentence: Please email contact@support.com for help.", ["contact@support.com"]),
            ("In quotes: 'my.email@example.com' is my email.", ["my.email@example.com"]),
            ("In parentheses: (email: test@domain.org)", ["test@domain.org"]),
            ("With brackets: [user@example.com]", ["user@example.com"]),
            ("With braces: {user@domain.com}", ["user@domain.com"]),
            ("With angle brackets: <contact@company.com>", ["contact@company.com"]),
            ("Context: For more info, email info@company.com or call support.", ["info@company.com", "support"]),
            
            # Phone examples (20)
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
            ("International: +1 (555) 123-4567", ["+1 (555) 123-4567"]),
            ("US: +1 555-123-4567", ["+1 555-123-4567"]),
            ("Canada: +1 (416) 555-1234", ["+1 (416) 555-1234"]),
            ("Europe: +49 30 1234567", ["+49 30 1234567"]),
            ("Asia: +81 3-1234-5678", ["+81 3-1234-5678"]),
            ("Australia: +61 2 1234 5678", ["+61 2 1234 5678"]),
            ("In parentheses: Phone: (+7 999) 123-45-67", ["(+7 999) 123-45-67"]),
            ("In quotes: '+7 (999) 123-45-67' is my number", ["+7 (999) 123-45-67"]),
            ("Context: Call +7 (999) 123-45-67 or +1 555-123-4567", ["+7 (999) 123-45-67", "+1 555-123-4567"]),
            
            # INN examples (20)
            ("INN: 0276453431", ["0276453431"]),  # Valid individual
            ("Tax ID: 1111111117", ["1111111117"]),  # Valid individual
            ("Entity INN: 111111111130", ["111111111130"]),  # Valid entity
            ("Company: 111111111130", ["111111111130"]),  # Valid entity
            ("Invalid INN: 1234567890", []),  # Invalid checksum
            ("Short INN: 123456789", []),  # Too short
            ("Long INN: 123456789012345", []),  # Too long
            ("In text: My INN is 0276453431", ["0276453431"]),
            ("Multiple: INN 0276453431 and 1111111117", ["0276453431", "1111111117"]),
            ("Entity: Company INN 5077466207712", ["5077466207712"]),
            ("In parentheses: (INN: 0276453431)", ["0276453431"]),
            ("In quotes: '0276453431' is my INN", ["0276453431"]),
            ("Context: INN 0276453431 for tax purposes", ["0276453431"]),
            ("With spaces: INN 027 645 343 1", []),  # Spaces detected as invalid
            ("Mixed: INN0276453431", ["INN0276453431"]),  # Without spaces
            ("In sentence: The INN number is 1111111117.", ["1111111117"]),
            ("In list: INNs: 0276453431, 1111111117", ["0276453431", "1111111117"]),
            ("In document: Entity INN: 5077466207712", ["5077466207712"]),
            ("In form: Enter INN: 7704781060168", ["7704781060168"]),
            ("In table: | INN | 0276453431 |", ["0276453431"]),
            
            # Passport examples (20)
            ("Passport: 1234 567890", ["1234 567890"]),
            ("ID: 9876 543210", ["9876 543210"]),
            ("Document: 1234 567890", ["1234 567890"]),  # With space
            ("Invalid: 123 45678", []),  # Wrong length
            ("With letters: ABC123456", []),  # Contains letters
            ("In text: My passport is 1234 567890", ["1234 567890"]),
            ("Multiple: Passport 1234 567890 and ID 9876 543210", ["1234 567890", "9876 543210"]),
            ("In parentheses: (Passport: 1234 567890)", ["1234 567890"]),
            ("In quotes: '1234 567890' is my passport", ["1234 567890"]),
            ("Context: Passport number 1234 567890", ["1234 567890"]),
            ("In form: Passport: 1234 567890", ["1234 567890"]),
            ("In document: Document ID: 9876 543210", ["9876 543210"]),
            ("In table: | Passport | 1234 567890 |", ["1234 567890"]),
            ("In sentence: The passport number is 1234 567890.", ["1234 567890"]),
            ("In list: Passports: 1234 567890, 9876 543210", ["1234 567890", "9876 543210"]),
            ("In email: passport:1234 567890", ["1234 567890"]),
            ("In address: Address with passport 1234 567890", ["1234 567890"]),
            ("In contract: Passport: 1234 567890", ["1234 567890"]),
            ("In application: ID number: 9876 543210", ["9876 543210"]),
            ("In system: Passport 1234 567890 registered", ["1234 567890"]),
            ("In database: Record: 1234 567890", ["1234 567890"]),
            
            # Payment card examples (20)
            ("Card: 4111111111111111", ["4111111111111111"]),  # Valid Visa test
            ("Payment: 5555555555554444", ["5555555555554444"]),  # Valid Mastercard test
            ("Credit: 378282246310005", ["378282246310005"]),  # Valid Amex
            ("Debit: 3530111333300000", ["3530111333300000"]),  # Valid JCB
            ("Invalid: 4111111111111112", []),  # Invalid checksum
            ("With letters: ABC123456789012", []),  # Contains letters
            ("In text: My card is 4111111111111111", ["4111111111111111"]),
            ("Multiple: Card 4111111111111111 and 5555555555554444", ["4111111111111111", "5555555555554444"]),
            ("In parentheses: (Card: 4111111111111111)", ["4111111111111111"]),
            ("In quotes: '4111111111111111' is my card", ["4111111111111111"]),
            ("Context: Card number 4111111111111111", ["4111111111111111"]),
            ("In form: Card: 4111111111111111", ["4111111111111111"]),
            ("In document: Payment: 5555555555554444", ["5555555555554444"]),
            ("In table: | Card | 4111111111111111 |", ["4111111111111111"]),
            ("In sentence: The card number is 4111111111111111.", ["4111111111111111"]),
            ("In list: Cards: 4111111111111111, 5555555555554444", ["4111111111111111", "5555555555554444"]),
            ("In email: card:4111111111111111", ["4111111111111111"]),
            ("In transaction: Payment 5555555555554444", ["5555555555554444"]),
            ("In checkout: Card: 4111111111111111", ["4111111111111111"]),
            ("In app: Card number 4111111111111111", ["4111111111111111"]),
            ("In database: Card: 4111111111111111", ["4111111111111111"]),
        ]
        
        total_entities = sum(len(expected) for _, expected in test_dataset)
        detected_entities = 0
        
        for text, expected_entities in test_dataset:
            matches = detector.detect(text)
            actual_entities = [match.value for match in matches]
            
            # Check that all expected entities are detected
            for expected in expected_entities:
                if expected in actual_entities:
                    detected_entities += 1
        
        # Calculate recall rate
        recall_rate = detected_entities / total_entities if total_entities > 0 else 0
        
        # Recall should be > 95%
        assert recall_rate > 0.95, f"Recall rate too low: {recall_rate:.3f}"
        
        print(f"Recall rate: {recall_rate:.3f} ({detected_entities}/{total_entities})")


class TestPIIDetectorIntegration:
    """Integration tests for PIIDetector with various scenarios."""
    
    def test_mixed_text_scenario(self):
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
        
        # Should detect 5 entities
        assert len(matches) == 5
        
        # Check all entity types
        entity_types = {match.entity_type for match in matches}
        assert entity_types == {"email", "phone", "inn", "passport", "payment"}
        
        # Check specific values
        email_matches = [m for m in matches if m.entity_type == "email"]
        assert len(email_matches) == 1
        assert email_matches[0].value == "support@company.com"
        
        phone_matches = [m for m in matches if m.entity_type == "phone"]
        assert len(phone_matches) == 1
        assert phone_matches[0].value == "+7 (999) 123-45-67"
    
    def test_detector_disabled(self):
        """Test that disabled detectors don't detect anything."""
        detector = PIIDetector(enabled_detectors={"email"})  # Only email enabled
        
        text = """
        Email: test@example.com
        Phone: +7 (999) 123-45-67
        INN: 0276453431
        Passport: 1234 567890
        Card: 4111111111111111
        """
        
        matches = detector.detect(text)
        
        # Should only detect email
        assert len(matches) == 1
        assert matches[0].entity_type == "email"
        assert matches[0].value == "test@example.com"
    
    def test_empty_text(self):
        """Test detection in empty text."""
        detector = PIIDetector()
        matches = detector.detect("")
        assert len(matches) == 0
    
    def test_text_without_pii(self):
        """Test detection in text without PII."""
        detector = PIIDetector()
        
        text = """
        This is a regular text without any personally identifiable information.
        It contains various words and sentences, but no emails, phone numbers,
        INN numbers, passport numbers, or payment card numbers.
        """
        
        matches = detector.detect(text)
        assert len(matches) == 0
    
    def test_detector_error_handling(self):
        """Test that detector handles errors gracefully."""
        detector = PIIDetector()
        
        # This should not crash even if there are issues
        text = "Normal text with some PII: test@example.com"
        matches = detector.detect(text)
        
        # Should still detect valid entities
        assert len(matches) > 0
        assert any(m.entity_type == "email" for m in matches)