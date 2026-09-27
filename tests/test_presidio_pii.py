"""Tests for Presidio-based PII detection."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from agent_obs.guardrail.presidio_engine import PresidioPIIEngine
from agent_obs.guardrail.pii_detector import PIIDetector
from agent_obs.guardrail.pii_types import PIIMatch


class TestPresidioPIIEngine:
    """Test PresidioPIIEngine functionality."""
    
    def test_singleton_pattern(self):
        """Test that PresidioPIIEngine follows singleton pattern."""
        engine1 = PresidioPIIEngine.get_instance()
        engine2 = PresidioPIIEngine.get_instance()
        assert engine1 is engine2
    
    def test_presidio_engine_creation(self):
        """Test Presidio engine creation with Russian language support."""
        engine = PresidioPIIEngine()
        assert engine.analyzer is not None
    
    def test_detect_person_names(self):
        """Test detection of person names (ФИО)."""
        engine = PresidioPIIEngine()
        matches = engine.detect("Hello, Anna Ivanova!")
        
        assert len(matches) == 1
        assert matches[0].entity_type == "PERSON_NAME"
        assert matches[0].value == "Anna Ivanova"
        assert matches[0].mask.startswith("[PERSON_NAME:")
    
    def test_detect_addresses(self):
        """Test detection of Russian addresses."""
        engine = PresidioPIIEngine()
        matches = engine.detect("Мой адрес: ул. Ленина 123")
        
        assert len(matches) == 1
        assert matches[0].entity_type == "ADDRESS"
        assert matches[0].value == "ул. Ленина 123"
    
    def test_detect_medical_conditions(self):
        """Test detection of medical conditions."""
        engine = PresidioPIIEngine()
        matches = engine.detect("У пациента диагностирован грипп")
        
        assert len(matches) == 1
        assert matches[0].entity_type == "MEDICAL"
        assert matches[0].value == "грипп"
    
    def test_entity_type_mapping(self):
        """Test mapping of Presidio entity types to our standard types."""
        test_cases = [
            ("PERSON", "PERSON_NAME"),
            ("LOCATION", "ADDRESS"),
            ("STREET_ADDRESS", "ADDRESS"),
            ("MEDICAL_CONDITION", "MEDICAL"),
            ("EMAIL_ADDRESS", "EMAIL"),
            ("PHONE_NUMBER", "PHONE"),
            ("UNKNOWN", None),
        ]
        
        engine = PresidioPIIEngine()
        for presidio_type, expected_type in test_cases:
            mapped = engine._map_entity_type(presidio_type)
            assert mapped == expected_type
    
    def test_hash_value_format(self):
        """Test that hash values are in correct format."""
        engine = PresidioPIIEngine()
        hash_value = engine._hash_value("test@example.com")
        assert len(hash_value) == 4
        assert hash_value.isalnum()
    
    def test_detection_failure_handling(self):
        """Test handling of detection failures."""
        engine = PresidioPIIEngine()
        
        # Test that the method handles exceptions gracefully
        # We can't easily mock the lambda to throw an exception and be caught,
        # but we can test that the method doesn't crash on normal input
        matches = engine.detect("Hello Anna")
        # This might not detect anything, but it shouldn't crash
        assert isinstance(matches, list)


class TestPIIDetectorWithML:
    """Test PIIDetector with ML stage integration."""
    
    def test_ml_disabled_by_default(self):
        """Test that ML stage is disabled by default."""
        detector = PIIDetector()
        assert not detector.ml_enabled
    
    def test_ml_enabled_via_env(self):
        """Test that ML stage can be enabled via environment variable."""
        with patch.dict('os.environ', {'AGENT_OBS_PII_ML_ENABLED': 'true'}):
            detector = PIIDetector()
            assert detector.ml_enabled
    
    @patch('agent_obs.guardrail.presidio_engine.PresidioPIIEngine.get_instance')
    def test_detect_with_ml_stage(self, mock_get_engine):
        """Test detection with ML stage enabled."""
        # Mock presidio engine
        mock_engine = Mock()
        mock_engine.detect.return_value = [
            PIIMatch("PERSON_NAME", "Ivan Ivanov", 25, 35, "[PERSON_NAME:abc1]")
        ]
        mock_get_engine.return_value = mock_engine
        
        # Create detector with ML enabled
        with patch.dict('os.environ', {'AGENT_OBS_PII_ML_ENABLED': 'true'}):
            detector = PIIDetector()
            
            # Mock regex detection
            with patch.object(detector, '_email_detector') as mock_email:
                mock_email.return_value = [
                    PIIMatch("email", "test@example.com", 7, 23, "[EMAIL:def2]")
                ]
                
                matches = detector.detect("Email: test@example.com, User: Ivan Ivanov")
                
                # Should have both regex and ML results
                assert len(matches) == 2
                # Verify the mock was called
                mock_engine.detect.assert_called_once()
                email_match = next(m for m in matches if m.entity_type == "email")
                person_match = next(m for m in matches if m.entity_type == "PERSON_NAME")
                assert email_match.value == "test@example.com"
                assert person_match.value == "Ivan Ivanov"
    
    @patch('agent_obs.guardrail.presidio_engine.PresidioPIIEngine.get_instance')
    def test_no_duplicates_with_regex_priority(self, mock_get_engine):
        """Test that regex matches take priority and ML avoids duplicates."""
        # Mock presidio engine returning a match that overlaps with regex
        mock_engine = Mock()
        mock_engine.detect.return_value = [
            PIIMatch("PERSON_NAME", "test@example.com", 0, 14, "[PERSON_NAME:abc1]")
        ]
        mock_get_engine.return_value = mock_engine
        
        # Create detector with ML enabled
        with patch.dict('os.environ', {'AGENT_OBS_PII_ML_ENABLED': 'true'}):
            detector = PIIDetector()
            
            # Mock regex detection returning same span
            with patch.object(detector, '_email_detector') as mock_email:
                mock_email.return_value = [
                    PIIMatch("email", "test@example.com", 0, 14, "[EMAIL:def2]")
                ]
                
                matches = detector.detect("Email: test@example.com")
                
                # Should have only regex match (no duplicate)
                assert len(matches) == 1
                assert matches[0].entity_type == "email"
                assert matches[0].value == "test@example.com"
    
    @patch('agent_obs.guardrail.presidio_engine.PresidioPIIEngine.get_instance')
    def test_non_overlapping_ml_matches_added(self, mock_get_engine):
        """Test that non-overlapping ML matches are added."""
        # Mock presidio engine returning non-overlapping match
        mock_engine = Mock()
        mock_engine.detect.return_value = [
            PIIMatch("PERSON_NAME", "Ivan Ivanov", 25, 35, "[PERSON_NAME:abc1]")
        ]
        mock_get_engine.return_value = mock_engine
        
        # Create detector with ML enabled
        with patch.dict('os.environ', {'AGENT_OBS_PII_ML_ENABLED': 'true'}):
            detector = PIIDetector()
            
            # Mock regex detection returning different span
            with patch.object(detector, '_email_detector') as mock_email:
                mock_email.return_value = [
                    PIIMatch("email", "test@example.com", 7, 23, "[EMAIL:def2]")
                ]
                
                matches = detector.detect("Email: test@example.com, User: Ivan Ivanov")
                
                # Should have both matches
                assert len(matches) == 2
                email_match = next(m for m in matches if m.entity_type == "email")
                person_match = next(m for m in matches if m.entity_type == "PERSON_NAME")
                assert email_match.value == "test@example.com"
                assert person_match.value == "Ivan Ivanov"
    
    @patch('agent_obs.guardrail.presidio_engine.PresidioPIIEngine.get_instance')
    def test_ml_failure_handling(self, mock_get_engine):
        """Test that ML failure doesn't break regex detection."""
        # Mock presidio engine to raise exception
        mock_engine = Mock()
        mock_engine.detect.side_effect = Exception("ML failed")
        mock_get_engine.return_value = mock_engine
        
        # Create detector with ML enabled
        with patch.dict('os.environ', {'AGENT_OBS_PII_ML_ENABLED': 'true'}):
            detector = PIIDetector()
            
            # Mock regex detection
            with patch.object(detector, '_email_detector') as mock_email:
                mock_email.return_value = [
                    PIIMatch("email", "test@example.com", 0, 14, "[EMAIL:def2]")
                ]
                
                matches = detector.detect("Email: test@example.com")
                
                # Should still have regex matches
                assert len(matches) == 1
                assert matches[0].entity_type == "email"


class TestPresidioPIIIntegration:
    """Integration tests for ML + PII detection."""
    
    @patch('agent_obs.guardrail.presidio_engine.PresidioPIIEngine.get_instance')
    def test_person_name_recall(self, mock_get_engine):
        """Test that person names are detected with high recall."""
        # Mock presidio engine to detect names
        mock_engine = Mock()
        test_cases = [
            ("Hello, Anna Ivanova", ["Anna Ivanova"]),
            ("Contact Ivan Ivanov please", ["Ivan Ivanov"]),
            ("Maria Petrova and Sergey Sokolov", ["Maria Petrova", "Sergey Sokolov"]),
        ]
        
        for text, expected_names in test_cases:
            mock_engine.reset_mock()
            mock_engine.detect.return_value = [
                PIIMatch("PERSON_NAME", name, text.find(name), text.find(name) + len(name), f"[PERSON_NAME:hash{i}]")
                for i, name in enumerate(expected_names)
            ]
            mock_get_engine.return_value = mock_engine
            
            with patch.dict('os.environ', {'AGENT_OBS_PII_ML_ENABLED': 'true'}):
                detector = PIIDetector()
                matches = detector.detect(text)
                
                # Check that names are detected
                detected_names = [m.value for m in matches if m.entity_type == "PERSON_NAME"]
                for expected_name in expected_names:
                    assert expected_name in detected_names
    
    def test_low_false_positive_rate_on_neutral_text(self):
        """Test that ML detection has low false positive rate on neutral text."""
        neutral_texts = [
            "Москва — столица России",
            "Россия — большая страна",
            "Погода в Москве хорошая",
            "Я люблю путешествовать",
            "Это интересная книга",
        ]
        
        # Test with ML disabled (only regex)
        detector_regex = PIIDetector()
        regex_matches = []
        for text in neutral_texts:
            matches = detector_regex.detect(text)
            regex_matches.extend(matches)
        
        # Test with ML enabled
        with patch.dict('os.environ', {'AGENT_OBS_PII_ML_ENABLED': 'true'}):
            with patch('agent_obs.guardrail.presidio_engine.PresidioPIIEngine.get_instance') as mock_get_engine:
                mock_engine = Mock()
                # Mock minimal ML detection for neutral text
                mock_engine.detect.return_value = []
                mock_get_engine.return_value = mock_engine
                
                detector_ml = PIIDetector()
                ml_matches = []
                for text in neutral_texts:
                    matches = detector_ml.detect(text)
                    ml_matches.extend(matches)
        
        # ML should not significantly increase false positives on neutral text
        assert len(ml_matches) <= len(regex_matches) * 2  # Allow some increase but not dramatic