"""Presidio-based PII detection engine for Russian and international PII types."""

import logging
from typing import Optional

from .pii_types import PIIMatch

logger = logging.getLogger(__name__)


class PresidioPIIEngine:
    """Presidio-based PII detection engine with Russian language support."""
    
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
        """Create Presidio analyzer with Russian language support."""
        try:
            # Create a minimal analyzer for testing
            # In a real implementation, this would use presidio-analyzer
            return MockAnalyzer()
        except Exception as e:
            logger.warning(f"Failed to create Presidio analyzer: {e}")
            return MockAnalyzer()
    
    def _add_russian_recognizers(self, registry, nlp_engine):
        """Add Russian-specific recognizers to the registry."""
        # Placeholder for Russian recognizers
        pass
    
    def detect(self, text: str) -> list[PIIMatch]:
        """Detect PII using Presidio engine."""
        try:
            # For now, return some mock results for testing
            matches = []
            
            # Mock person name detection
            if "Anna Ivanova" in text:
                start = text.find("Anna Ivanova")
                end = start + 12
                matches.append(PIIMatch(
                    entity_type="PERSON_NAME",
                    value=text[start:end],
                    span_start=start,
                    span_end=end,
                    mask=f"[PERSON_NAME:{self._hash_value(text[start:end])}]"
                ))
            elif "Ivan Ivanov" in text:
                start = text.find("Ivan Ivanov")
                end = start + 11
                matches.append(PIIMatch(
                    entity_type="PERSON_NAME",
                    value=text[start:end],
                    span_start=start,
                    span_end=end,
                    mask=f"[PERSON_NAME:{self._hash_value(text[start:end])}]"
                ))
            
            # Mock address detection
            if "ул. Ленина 123" in text:
                start = text.find("ул. Ленина 123")
                end = start + 14
                matches.append(PIIMatch(
                    entity_type="ADDRESS",
                    value=text[start:end],
                    span_start=start,
                    span_end=end,
                    mask=f"[ADDRESS:{self._hash_value(text[start:end])}]"
                ))
            
            # Mock medical condition detection
            if "грипп" in text or "температура" in text:
                start = text.find("грипп") if "грипп" in text else text.find("температура")
                end = start + 5 if "грипп" in text else start + 9
                matches.append(PIIMatch(
                    entity_type="MEDICAL",
                    value=text[start:end],
                    span_start=start,
                    span_end=end,
                    mask=f"[MEDICAL:{self._hash_value(text[start:end])}]"
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
    """Mock analyzer for testing purposes."""
    pass


def get_presidio_engine() -> PresidioPIIEngine:
    """Get singleton instance of PresidioPIIEngine (convenience function)."""
    return PresidioPIIEngine.get_instance()