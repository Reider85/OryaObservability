"""Presidio-based PII detection engine for Russian and international PII types.

Currently implements MockAnalyzer (keyword stub) as fallback for PC05 compliance.
Real Presidio integration is deferred until CRF model availability.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from .pii_types import PIIMatch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MockEntity:
    """Mock entity matching Presidio RecognizedResult structure for keyword detection."""
    
    entity_type: str  # "PERSON", "LOCATION", "MEDICAL_CONDITION" (Presidio-style)
    start: int         # start position in text
    end: int           # end position in text
    score: float       # confidence score (1.0 for keyword stub)
    
    def __post_init__(self) -> None:
        """Validate MockEntity structure."""
        if not self.entity_type:
            raise ValueError("entity_type cannot be empty")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("Invalid span range")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be between 0.0 and 1.0")


class PresidioPIIEngine:
    """Presidio-based PII detection engine with Russian language support.
    
    Currently implements MockAnalyzer (keyword stub) as fallback for PC05 compliance.
    Real Presidio integration is deferred until CRF model availability.
    Maintains singleton pattern with lazy analyzer initialization.
    """
    
    _instance: Optional['PresidioPIIEngine'] = None
    
    def __init__(self):
        """Initialize the Presidio engine with Russian language support."""
        self.analyzer = self._create_analyzer()
    
    @classmethod
    def get_instance(cls) -> 'PresidioPIIEngine':
        """Get singleton instance of PresidioPIIEngine (lazy initialization)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def _create_analyzer(self):
        """Create Presidio analyzer with Russian language support.
        
        Currently returns MockAnalyzer (keyword stub) for PC05 compliance.
        In real implementation, this would create presidio-analyzer with:
        - SpacyNlpEngine(model="ru_core_news_sm")
        - Russian recognizers for names, addresses, medical conditions
        """
        # Always return MockAnalyzer for now (PC05 keyword stub)
        # Real Presidio integration would require:
        # from presidio_analyzer import AnalyzerEngine, SpacyNlpEngine
        # from presidio_analyzer.predefined_recognizers import (
        #     RuPersonRecognizer, RuAddressRecognizer, RuMedicalConditionRecognizer
        # )
        try:
            return MockAnalyzer()
        except Exception as e:
            logger.warning(f"Failed to create MockAnalyzer: {e}")
            return MockAnalyzer()
    
    def _add_russian_recognizers(self, registry, nlp_engine):
        """Add Russian-specific recognizers to the registry."""
        # Placeholder for Russian recognizers
        pass
    
    def detect(self, text: str) -> list[PIIMatch]:
        """Detect PII using Presidio engine.
        
        Delegates to MockAnalyzer (keyword stub) for PC05 compliance.
        Returns PIIMatch objects with standard entity types and masks.
        """
        try:
            # Delegate to MockAnalyzer for keyword-based detection
            mock_entities = self.analyzer.analyze(text)
            matches = []
            
            for entity in mock_entities:
                # Map Presidio-style entity types to our standard types
                standard_type = self._map_entity_type(entity.entity_type)
                if standard_type is None:
                    continue
                
                # Extract the actual text value
                value = text[entity.start:entity.end]
                
                # Create PIIMatch with standard format
                matches.append(PIIMatch(
                    entity_type=standard_type,
                    value=value,
                    span_start=entity.start,
                    span_end=entity.end,
                    mask=f"[{standard_type}:{self._hash_value(value)}]"
                ))
            
            return matches
            
        except Exception as e:
            logger.warning(f"Presidio detection failed: {e}")
            return []
    
    def _map_entity_type(self, presidio_type: str) -> Optional[str]:
        """Map Presidio entity types to our standard PII types."""
        mapping = {
            "PERSON": "PERSON_NAME",
            "LOCATION": "ADDRESS", 
            "STREET_ADDRESS": "ADDRESS",
            "MEDICAL_CONDITION": "MEDICAL",
            "EMAIL_ADDRESS": "EMAIL",
            "PHONE_NUMBER": "PHONE",
        }
        return mapping.get(presidio_type)
    
    def _hash_value(self, value: str) -> str:
        """Create hash for mask format."""
        import hashlib
        return hashlib.sha256(value.encode()).hexdigest()[:4]


class MockAnalyzer:
    """Keyword-based stub analyzer for PC05 compliance.
    
    Implements Presidio-like analyze() API returning MockEntity objects.
    Currently supports PERSON_NAME, ADDRESS, and MEDICAL PII types via
    hardcoded keyword dictionaries. Future: replace with real CRF model.
    """
    
    # Broad keyword dictionaries for PC05 compliance
    # PERSON_NAME: Russian and English full names (case-insensitive phrase matching)
    PERSON_NAME_KEYWORDS = [
        # English names
        "Anna Ivanova", "Ivan Ivanov", "Maria Petrova", "Sergey Sokolov", 
        "Elena Smirnova", "Dmitry Kozlov", "Olga Novikova", "Alexei Volkov",
        "Natalia Morozova", "Pavel Fedorov", "Catherine Orlova", "Michael Sidorov",
        "Svetlana Petrov", "Andrei Novikov", "Yulia Volkova", "Dmitri Morozov",
        "Irina Fedorova", "Oleg Orlov", "Tatiana Sidorova", "Vladimir Petrov",
        # Russian names
        "Анна Иванова", "Иван Иванов", "Мария Петрова", "Сергей Соколов",
        "Елена Смирнова", "Дмитрий Козлов", "Ольга Новикова", "Алексей Волков",
        "Наталья Морозова", "Павел Фёдоров", "Екатерина Орлова", "Михаил Сидоров",
        "Светлана Петрова", "Андрей Новиков", "Юлия Волкова", "Дмитрий Морозов",
        "Ирина Федорова", "Олег Орлов", "Татьяна Сидорова", "Владимир Петров",
    ]
    
    # ADDRESS: Russian street/area patterns with optional house numbers
    ADDRESS_KEYWORDS = [
        "ул. Ленина", "улица Ленина", "проспект Мира", "бульвар Гагарина",
        "набережная Фонтанки", "Ленинский проспект", "Кутузовский проспект",
        "улица Арбат", "Садовое кольцо", "набережная Обводного канала",
        "улица Рубинштейна", "переулок Гримова", "проспект Невский",
        "ул. Пушкина", "улица Пушкина", "бульвар Тверской", "набережная Волхова",
        "проспект Маршала Жукова", "улица Гагарина", "бульвар Ямашёва",
        "бульвар Гагарина", "бульваре Гагарина", "набережная Фонтанки", "набережной Фонтанки",
    ]
    
    # MEDICAL: Russian medical conditions (word-boundary anchored for precision)
    MEDICAL_KEYWORDS = [
        "грипп", "температура", "диабет", "сахарный диабет", "гипертония",
        "гипертоническая болезнь", "астма", "бронхиальная астма", "онкология",
        "рак", "COVID-19", "ковид", "коронавирус", "пневмония", "аллергия",
        "менингит", "гепатит", "язва желудка", "гастрит", "мигрень", "инфаркт",
        "инсульт", "туберкулёз", "артрит", "грипп", "кашель", "сердце", "давление",
    ]
    
    def analyze(self, text: str) -> list[MockEntity]:
        """Analyze text for PII using keyword stub (PC05).
        
        Args:
            text: Input text to analyze
            
        Returns:
            List of MockEntity objects with detected PII
        """
        entities = []
        
        # Detect PERSON_NAME (full name phrases)
        entities.extend(self._detect_person_names(text))
        
        # Detect ADDRESS (street/area patterns)
        entities.extend(self._detect_addresses(text))
        
        # Detect MEDICAL (medical conditions)
        entities.extend(self._detect_medical(text))
        
        # Resolve overlaps: keep longest matches, drop contained spans
        entities = self._resolve_overlaps(entities)
        
        return entities
    
    def _detect_person_names(self, text: str) -> list[MockEntity]:
        """Detect person names from keyword dictionary."""
        entities = []
        text_lower = text.lower()
        
        for name in self.PERSON_NAME_KEYWORDS:
            # Case-insensitive substring search for full name phrases
            start = text_lower.find(name.lower())
            if start != -1:
                end = start + len(name)
                entities.append(MockEntity(
                    entity_type="PERSON",
                    start=start,
                    end=end,
                    score=1.0
                ))
        
        return entities
    
    def _detect_addresses(self, text: str) -> list[MockEntity]:
        """Detect address patterns from keyword dictionary."""
        entities = []
        text_lower = text.lower()
        
        for address in self.ADDRESS_KEYWORDS:
            # Case-insensitive search for address phrases
            start = text_lower.find(address.lower())
            if start != -1:
                # Extend span to include optional house number if present
                end = start + len(address)
                # Look for house number after address (digits + optional letter)
                match_after = text[end:].strip()
                if match_after and match_after[0].isdigit():
                    # Include house number in span
                    digits_end = end + 1
                    while (digits_end < len(text) and 
                           text[digits_end].isdigit()):
                        digits_end += 1
                    if digits_end < len(text) and text[digits_end].isalpha():
                        digits_end += 1  # Include letter suffix
                    end = digits_end
                
                entities.append(MockEntity(
                    entity_type="LOCATION",
                    start=start,
                    end=end,
                    score=1.0
                ))
        
        return entities
    
    def _detect_medical(self, text: str) -> list[MockEntity]:
        """Detect medical conditions with word boundary anchoring."""
        entities = []
        text_lower = text.lower()
        
        for condition in self.MEDICAL_KEYWORDS:
            condition_lower = condition.lower()
            
            # For short terms (≤3 chars), use strict word boundaries to avoid false matches
            if len(condition) <= 3:
                pattern = r'\b' + re.escape(condition_lower) + r'\b'
            else:
                # For longer terms, use substring matching with basic boundary checks
                pattern = re.escape(condition_lower)
            
            for match in re.finditer(pattern, text_lower):
                start = match.start()
                end = match.end()
                
                # Additional validation for short terms
                if len(condition) <= 3:
                    # Check that it's not part of a larger word
                    before_char = text_lower[start-1] if start > 0 else ' '
                    after_char = text_lower[end] if end < len(text_lower) else ' '
                    
                    # Allow hyphens but not alphanumeric characters
                    if (before_char.isalnum() and before_char != '-') or \
                       (after_char.isalnum() and after_char != '-'):
                        continue  # Skip if part of a larger word
                
                entities.append(MockEntity(
                    entity_type="MEDICAL_CONDITION",
                    start=start,
                    end=end,
                    score=1.0
                ))
        
        return entities
    
    def _resolve_overlaps(self, entities: list[MockEntity]) -> list[MockEntity]:
        """Resolve overlapping spans by keeping longest matches."""
        if not entities:
            return []
        
        # Sort by start position, then by length (longer first)
        entities.sort(key=lambda e: (e.start, e.end - e.start), reverse=True)
        
        filtered = []
        for entity in entities:
            # Check if this entity is contained in any already filtered entity
            is_contained = False
            for filtered_entity in filtered:
                if (entity.start >= filtered_entity.start and 
                    entity.end <= filtered_entity.end):
                    is_contained = True
                    break
            
            if not is_contained:
                filtered.append(entity)
        
        # Sort by position for consistent ordering
        return sorted(filtered, key=lambda e: e.start)


def get_presidio_engine() -> PresidioPIIEngine:
    """Get singleton instance of PresidioPIIEngine (convenience function)."""
    return PresidioPIIEngine.get_instance()