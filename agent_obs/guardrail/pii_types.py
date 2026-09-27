"""Shared PII match dataclass for all PII detectors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PIIMatch:
    """A detected PII entity with metadata."""
    
    entity_type: str  # "email", "phone", "inn", "passport", "payment", "PERSON_NAME", "ADDRESS", "MEDICAL"
    value: str  # original text value
    span_start: int  # start position in text
    span_end: int  # end position in text
    mask: str  # formatted mask like "[EMAIL:5f3a]"
    
    def __post_init__(self) -> None:
        """Validate the PIIMatch structure."""
        if not self.entity_type:
            raise ValueError("entity_type cannot be empty")
        if not self.value:
            raise ValueError("value cannot be empty")
        if self.span_start < 0 or self.span_end <= self.span_start:
            raise ValueError("Invalid span range")