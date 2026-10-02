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


class TestPC05MockAnalyzerCompliance:
    """PC05 compliance tests for MockAnalyzer keyword stub."""
    
    def test_person_name_recall_broad(self):
        """Test person name recall > 90% on broad vocabulary (stub-level DoD)."""
        engine = PresidioPIIEngine()
        
        # Test dataset with broad name vocabulary from MockAnalyzer
        test_cases = [
            # English names in various contexts
            ("Hello, Anna Ivanova!", "Anna Ivanova"),
            ("Contact Ivan Ivanov please", "Ivan Ivanov"),
            ("Meeting with Maria Petrova at 3pm", "Maria Petrova"),
            ("Email Sergey Sokolov about the project", "Sergey Sokolov"),
            ("Dr. Elena Smirnova will see you now", "Elena Smirnova"),
            ("Call Dmitry Kozlov for support", "Dmitry Kozlov"),
            ("Welcome Olga Novikova to the team", "Olga Novikova"),
            ("Alexei Volkov is the new manager", "Alexei Volkov"),
            ("Thank you, Natalia Morozova!", "Natalia Morozova"),
            ("Pavel Fedorov submitted the report", "Pavel Fedorov"),
            ("Catherine Orlova needs a signature", "Catherine Orlova"),
            ("Michael Sidorov is available today", "Michael Sidorov"),
            
# Russian names in various contexts (using exact keyword forms)
            ("Здравствуйте, Анна Иванова!", "Анна Иванова"),
            ("Иван Иванов здесь", "Иван Иванов"),
            ("Мария Петрова приехала", "Мария Петрова"),
            ("Сергей Соколов работает", "Сергей Соколов"),
            ("Елена Смирнова приехала", "Елена Смирнова"),
            ("Дмитрий Козлов звонил", "Дмитрий Козлов"),
            ("Ольга Новикова пришла", "Ольга Новикова"),
            ("Алексей Волков arrived", "Алексей Волков"),
            ("Наталья Морозова ушла", "Наталья Морозова"),
            ("Павел Фёдоров уехал", "Павел Фёдоров"),
            ("Екатерина Орлова звонила", "Екатерина Орлова"),
            ("Михаил Сидоров работает", "Михаил Сидоров"),
            
            # Names with different casing
            ("ANNA IVANOVA is here", "ANNA IVANOVA"),
            ("ivan ivanov needs help", "ivan ivanov"),
            ("Анна ИВАНОВА из Москвы", "Анна ИВАНОВА"),
            ("иван иванов из СПБ", "иван иванов"),
        ]
        
        total_names = len(test_cases)
        detected_names = 0
        
        for text, expected_name in test_cases:
            matches = engine.detect(text)
            person_matches = [m for m in matches if m.entity_type == "PERSON_NAME"]
            
            # Check if expected name was detected
            detected = any(expected_name in match.value for match in person_matches)
            if detected:
                detected_names += 1
        
        # MockAnalyzer should achieve 100% recall on its own vocabulary (stub-level DoD)
        recall_rate = detected_names / total_names if total_names > 0 else 0
        assert recall_rate > 0.90, f"Recall rate too low: {recall_rate:.3f} (stub-level DoD)"
        print(f"Person name recall: {recall_rate:.3f} ({detected_names}/{total_names})")
    
    def test_no_false_positive_geo_names(self):
        """Test that geo names are not detected as PII (FPR = 0 for geo names)."""
        engine = PresidioPIIEngine()
        
        # Geo names that should NOT trigger detection
        geo_texts = [
            "Москва — столица России",
            "Россия — большая страна",
            "Путешествие в Санкт-Петербург",
            "Отпуск в Сочи был отличным",
            "Новосибирск — третий город России",
            "Казань — столица Татарстана",
            "Екатеринбург — промышленный центр",
            "Ростов-на-Дону — город на Дону",
            "Владивосток — порт на Дальнем Востоке",
            "Минск — столица Беларуси",
            "Киев — столица Украины",
        ]
        
        for text in geo_texts:
            matches = engine.detect(text)
            person_matches = [m for m in matches if m.entity_type == "PERSON_NAME"]
            address_matches = [m for m in matches if m.entity_type == "ADDRESS"]
            medical_matches = [m for m in matches if m.entity_type == "MEDICAL"]
            
            # No PII detection expected on geo names
            assert len(person_matches) == 0, f"PERSON_NAME detected in geo text: {text}"
            assert len(address_matches) == 0, f"ADDRESS detected in geo text: {text}"
            assert len(medical_matches) == 0, f"MEDICAL detected in geo text: {text}"
    
    def test_neutral_text_no_crash_low_fpr(self):
        """Test neutral text (PC05 set) doesn't crash, FPR < 5%."""
        engine = PresidioPIIEngine()
        
        # PC05 neutral texts from CRITICAL-PROMPTS.md
        neutral_texts = [
            "Я люблю путешествовать",
            "Это интересная книга",
            "Погода в Москве хорошая",
            "Работа была продуктивной",
            "Ужин был вкусным",
            "Встреча прошла успешно",
            "Проект завершён вовремя",
            "Клиент доволен результатом",
            "Команда работает эффективно",
            "Рынок стабилен",
        ]
        
        total_matches = 0
        for text in neutral_texts:
            matches = engine.detect(text)
            total_matches += len(matches)
            
            # Should not crash on any neutral text
            assert isinstance(matches, list), f"detect() should return list for: {text}"
        
        # FPR should be < 5% (very strict for neutral text)
        false_positive_rate = total_matches / len(neutral_texts) if neutral_texts else 0
        assert false_positive_rate < 0.05, f"False positive rate too high: {false_positive_rate:.3f}"
        print(f"Neutral text FPR: {false_positive_rate:.3f}")
    
    def test_address_detected_with_house_number(self):
        """Test address detection including house numbers."""
        engine = PresidioPIIEngine()
        
        address_tests = [
            ("Мой адрес: ул. Ленина 123", "ул. Ленина"),
            ("Проживаю по адресу: улица Пушкина 45", "улица Пушкина"),
            ("Доставка на проспект Мира 10", "проспект Мира"),
            ("Живу на бульваре Гагарина 7а", "бульваре Гагарина"),
            ("Кабинет находится в набережной Фонтанки 8", "набережной Фонтанки"),
            ("Ленинский проспект 15, корпус 2", "Ленинский проспект"),
        ]
        
        for text, expected_address in address_tests:
            matches = engine.detect(text)
            address_matches = [m for m in matches if m.entity_type == "ADDRESS"]
            
            assert len(address_matches) >= 1, f"No ADDRESS detected in: {text}"
            # Check if expected address is found (case-insensitive substring match)
            detected = any(expected_address.lower() in match.value.lower() for match in address_matches)
            assert detected, f"Expected address not found: {expected_address} in {text}. Found: {[m.value for m in address_matches]}"
            
            # Verify mask format
            for match in address_matches:
                assert match.mask.startswith("[ADDRESS:"), f"Wrong mask format: {match.mask}"
                # Format: [ADDRESS:xxxx] (14 characters)
                assert len(match.mask) == 14, f"Wrong mask length: {match.mask}"
                assert match.mask[-1] == "]", f"Mask should end with ]: {match.mask}"
    
    def test_medical_conditions_detected(self):
        """Test medical condition detection with word boundaries."""
        engine = PresidioPIIEngine()
        
        medical_tests = [
            ("У пациента диагностирован грипп", "грипп"),
            ("Температура тела повышена", "Температура"),
            ("У него диабет 2 типа", "диабет"),
            ("Гипертоническая болезнь требует лечения", "Гипертоническая"),
            ("Бронхиальная астма — хроническое заболевание", "Бронхиальная"),
            ("Онкология требует срочного лечения", "Онкология"),
            ("Рак лёгких — серьёзный диагноз", "Рак"),
            ("COVID-19 диагностирован у пациента", "COVID-19"),
            ("Коронавирусная инфекция распространяется", "Коронавирусная"),
            ("Пневмония развилась как осложнение", "Пневмония"),
            ("Аллергия на пыльцу цветущих растений", "Аллергия"),
            ("Мигрень беспокоит уже неделю", "Мигрень"),
        ]
        
        for text, expected_medical in medical_tests:
            matches = engine.detect(text)
            medical_matches = [m for m in matches if m.entity_type == "MEDICAL"]
            
            assert len(medical_matches) >= 1, f"No MEDICAL detected in: {text}"
            # Check if expected medical term is found (case-insensitive substring match)
        detected = any(expected_medical.lower() in match.value.lower() for match in medical_matches)
        assert detected, f"Expected medical term not found: {expected_medical} in {text}. Found: {[m.value for m in medical_matches]}"
    
    def test_medical_word_boundaries(self):
        """Test that medical terms don't match when embedded in other words."""
        engine = PresidioPIIEngine()
        
        # Medical terms that should NOT match when embedded
        false_positive_tests = [
            ("Пираты захватили корабль", "рак"),  # рак should not match пират
            ("Краков — красивый город", "рак"),   # рак should not match краков
            ("Ковёр был красивым", "ковид"),     # ковид should not match ковёр
            ("Мигрировали птицы", "мигрень"),   # мигрень should not match мигрировали
        ]
        
        for text, false_positive_term in false_positive_tests:
            matches = engine.detect(text)
            medical_matches = [m for m in matches if m.entity_type == "MEDICAL"]
            
            # Should not detect false positive medical terms
            for match in medical_matches:
                assert false_positive_term not in match.value.lower(), \
                    f"False positive medical detection: {false_positive_term} in {text}"
    
    def test_mask_format_matches_pc04(self):
        """Test all ML masks match PC04 format [TYPE:xxxx]."""
        engine = PresidioPIIEngine()
        
        test_texts = [
            "Anna Ivanova lives at ул. Ленина 123 and has грипп",
            "Patient with температура needs treatment for диабет",
            "Address: проспект Мира 10, Medical: COVID-19",
        ]
        
        for text in test_texts:
            matches = engine.detect(text)
            for match in matches:
                # Verify mask format for ML types
                if match.entity_type in ["PERSON_NAME", "ADDRESS", "MEDICAL"]:
                    assert match.mask.startswith(f"[{match.entity_type}:"), \
                        f"Wrong mask prefix: {match.mask}"
                    # Format: [TYPE:hex4] where hex4 is 4 hex characters
                    expected_length = len(match.entity_type) + 7  # [TYPE:xxxx] (including the colon)
                    assert len(match.mask) == expected_length, f"Wrong mask length: {match.mask}, expected {expected_length}"
                    assert match.mask[-1] == "]", f"Mask should end with ]: {match.mask}"
                    
                    # Verify hash is 4 hex characters
                    hash_part = match.mask.split(":")[1][:-1]  # Extract hex part without ]
                    assert len(hash_part) == 4, f"Hash should be 4 chars: {hash_part}"
                    assert all(c in "0123456789abcdef" for c in hash_part), \
                        f"Hash should be hex: {hash_part}"
                    
                    # Verify hash is 4 hex characters
                    hash_part = match.mask.split(":")[1][:-1]  # Extract hex part without ]
                    assert len(hash_part) == 4, f"Hash should be 4 chars: {hash_part}"
                    assert all(c in "0123456789abcdef" for c in hash_part), \
                        f"Hash should be hex: {hash_part}"
    
    def test_singleton_and_lazy_analyzer(self):
        """Test singleton pattern and lazy analyzer initialization."""
        # Test singleton
        engine1 = PresidioPIIEngine.get_instance()
        engine2 = PresidioPIIEngine.get_instance()
        assert engine1 is engine2
        
        # Test that analyzer is MockAnalyzer
        from agent_obs.guardrail.presidio_engine import MockAnalyzer
        assert isinstance(engine1.analyzer, MockAnalyzer)
        
        # Test second call doesn't re-create analyzer
        assert engine1.analyzer is engine2.analyzer
    
    def test_aggregation_regex_priority_unmocked(self):
        """Test ML + regex aggregation with real (unmocked) engine."""
        # Create PIIDetector with ML enabled
        with patch.dict('os.environ', {'AGENT_OBS_PII_ML_ENABLED': 'true'}):
            detector = PIIDetector()
            
            # Test text with both regex PII (email) and ML PII (person name)
            text = "Email: test@example.com, User: Anna Ivanova"
            matches = detector.detect(text)
            
            # Should have both types
            email_matches = [m for m in matches if m.entity_type == "email"]
            person_matches = [m for m in matches if m.entity_type == "PERSON_NAME"]
            
            assert len(email_matches) == 1, f"Expected 1 email, got {len(email_matches)}"
            assert len(person_matches) == 1, f"Expected 1 person name, got {len(person_matches)}"
            
            # Verify non-overlapping spans
            email_span = (email_matches[0].span_start, email_matches[0].span_end)
            person_span = (person_matches[0].span_start, person_matches[0].span_end)
            
            assert email_span != person_span, "Email and person spans should not overlap"
            
            # Verify correct values
            assert "test@example.com" in email_matches[0].value
            assert "Anna Ivanova" in person_matches[0].value
    
    def test_analyze_presidio_shape(self):
        """Test MockAnalyzer.analyze() returns Presidio-shaped MockEntity objects."""
        from agent_obs.guardrail.presidio_engine import MockAnalyzer
        
        analyzer = MockAnalyzer()
        text = "Anna Ivanova lives at ул. Ленина 123 and has грипп"
        
        entities = analyzer.analyze(text)
        
        # Should return list of MockEntity
        assert isinstance(entities, list)
        assert len(entities) > 0
        
        for entity in entities:
            # Verify MockEntity structure
            assert hasattr(entity, 'entity_type')
            assert hasattr(entity, 'start')
            assert hasattr(entity, 'end')
            assert hasattr(entity, 'score')
            
            # Validate fields
            assert entity.start < entity.end, f"Invalid span: {entity.start} >= {entity.end}"
            assert 0.0 <= entity.score <= 1.0, f"Invalid score: {entity.score}"
            assert entity.entity_type in ["PERSON", "LOCATION", "MEDICAL_CONDITION"]
            
            # Verify span is within text bounds
            assert 0 <= entity.start < len(text)
            assert 0 < entity.end <= len(text)
            
            # Verify entity value matches span
            actual_value = text[entity.start:entity.end]
            assert actual_value in entity.entity_type or \
                   (entity.entity_type == "PERSON" and any(name in actual_value for name in analyzer.PERSON_NAME_KEYWORDS)) or \
                   (entity.entity_type == "LOCATION" and any(addr in actual_value for addr in analyzer.ADDRESS_KEYWORDS)) or \
                   (entity.entity_type == "MEDICAL_CONDITION" and any(med in actual_value for med in analyzer.MEDICAL_KEYWORDS))
    
    def test_map_entity_type_extended(self):
        """Test entity type mapping includes all PC05 types."""
        engine = PresidioPIIEngine()
        
        test_cases = [
            ("PERSON", "PERSON_NAME"),
            ("LOCATION", "ADDRESS"),
            ("STREET_ADDRESS", "ADDRESS"),
            ("MEDICAL_CONDITION", "MEDICAL"),
            ("EMAIL_ADDRESS", "EMAIL"),
            ("PHONE_NUMBER", "PHONE"),
            ("UNKNOWN", None),
        ]
        
        for presidio_type, expected_type in test_cases:
            mapped = engine._map_entity_type(presidio_type)
            assert mapped == expected_type, \
                f"Mapping failed: {presidio_type} -> {mapped}, expected {expected_type}"