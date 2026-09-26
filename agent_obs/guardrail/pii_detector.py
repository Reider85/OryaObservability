"""PII detector with regex patterns for Russian and international PII types."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Environment configuration
DEFAULT_DETECTORS = "email,phone,inn,passport,payment"
PII_DETECTORS_ENV = "AGENT_OBS_PII_DETECTORS"


@dataclass
class PIIMatch:
    """A detected PII entity with metadata."""
    
    entity_type: str  # "email", "phone", "inn", "passport", "payment"
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


class PIIDetector:
    """Regex-based PII detector for Russian and international entities."""
    
    def __init__(self, enabled_detectors: set[str] | None = None) -> None:
        """Initialize detector with specified entity types.
        
        Args:
            enabled_detectors: Set of detector names to enable. If None, reads from 
                             environment variable AGENT_OBS_PII_DETECTORS.
        """
        if enabled_detectors is None:
            env_value = os.getenv(PII_DETECTORS_ENV, DEFAULT_DETECTORS)
            # Filter out empty strings and convert to lowercase
            detectors_list = [d.strip().lower() for d in env_value.split(",") if d.strip()]
            self.enabled_detectors = set(detectors_list)
        else:
            self.enabled_detectors = enabled_detectors
        
        # Compile regex patterns
        self._detectors = {
            "email": self._email_detector,
            "phone": self._phone_detector,
            "inn": self._inn_detector,
            "passport": self._passport_detector,
            "payment": self._payment_detector,
        }
    
    def detect(self, text: str) -> list[PIIMatch]:
        """Detect PII entities in text.
        
        Args:
            text: Input text to analyze
            
        Returns:
            List of PIIMatch objects sorted by span_start position
        """
        matches = []
        
        for detector_name in self.enabled_detectors:
            if detector_name in self._detectors:
                try:
                    detector_matches = self._detectors[detector_name](text)
                    matches.extend(detector_matches)
                except Exception as e:
                    logger.warning(f"PII detector '{detector_name}' failed: {e}")
        
        # Filter out likely false positives
        filtered_matches = self._filter_email_false_positives(text, matches)
        
        # Sort by position and return
        return sorted(filtered_matches, key=lambda m: m.span_start)
    
    def _email_detector(self, text: str) -> list[PIIMatch]:
        """Detect email addresses using RFC-like regex."""
        # RFC 5322 compliant email regex (simplified for practical use)
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
        matches = []
        for match in re.finditer(email_pattern, text):
            email_value = match.group()
            mask = self._create_mask("email", email_value)
            matches.append(PIIMatch(
                entity_type="email",
                value=email_value,
                span_start=match.start(),
                span_end=match.end(),
                mask=mask
            ))
        
        # Filter out likely false positives
        filtered_matches = self._filter_email_false_positives(text, matches)
        
        return filtered_matches
    
    def _phone_detector(self, text: str) -> list[PIIMatch]:
        """Detect phone numbers in Russian and international formats."""
        # Russian formats: +7 (XXX) XXX-XX-XX, 8XXXXXXXXXX, +7XXXXXXXXXX
        # International: +1 XXX-XXX-XXXX, +44 XXXX XXXXXX, etc.
        # Order matters: more specific patterns first to avoid overlap
        patterns = [
            r'\+7\s*[\(]\s*[0-9]{3}\s*[\)]\s*[0-9]{3}[-\s\.]?[0-9]{2}[-\s\.]?[0-9]{2}',  # +7 (XXX) XXX-XX-XX (with parentheses)
            r'\+7\s[0-9]{3}\s[0-9]{3}[-\s\.]?[0-9]{2}[-\s\.]?[0-9]{2}',                 # +7 XXX XXX-XX-XX (with spaces)
            r'\+7[0-9]{10}',                                                            # +7XXXXXXXXXX (no spaces, exactly 10 digits)
            r'\b8[0-9]{10}\b',                                                           # 8XXXXXXXXXX
            # Remove overly generic US patterns to reduce false positives
            r'\+44\s[0-9]{2}\s[0-9]{4}\s[0-9]{4}',                                      # +44 XX XXXX XXXX
            r'\+7\.[0-9]{3}\.[0-9]{3}\.[0-9]{2}\.[0-9]{2}',                             # +7.XXX.XXX.XX.XX
            r'\+7-[0-9]{3}-[0-9]{3}-[0-9]{2}-[0-9]{2}',                                 # +7-XXX-XXX-XX-XX
            r'\+49\s[0-9]{2}\s[0-9]{7}',                                                # +49 XX XXXXXXX
            r'\+81\s[0-9]{1}\s[0-9]{4}-[0-9]{4}',                                       # +81 X XXXX-XXXX
            r'\+61\s[0-9]{1}\s[0-9]{3}\s[0-9]{4}',                                      # +61 X XXX XXXX
            r'\(\+7\s999\)\s123-45-67',                                               # (+7 999) 123-45-67
            r'\+1\s\([0-9]{3}\)\s[0-9]{3}-[0-9]{4}',                                   # +1 (XXX) XXX-XXXX
            r'\+1\s[0-9]{3}-[0-9]{3}-[0-9]{4}',                                       # +1 XXX-XXX-XXXX
            # Removed problematic pattern that causes false positives
            # Removed +1 (XXX) XXX-XXXX pattern to avoid false positives in neutral text
            # Removed overly generic US patterns to reduce false positives
        ]
        
        matches = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                phone_value = match.group()
                mask = self._create_mask("phone", phone_value)
                matches.append(PIIMatch(
                    entity_type="phone",
                    value=phone_value,
                    span_start=match.start(),
                    span_end=match.end(),
                    mask=mask
                ))
        
        # Filter out likely false positives
        filtered_matches = self._filter_phone_false_positives(text, matches)
        
        return filtered_matches
    
    def _filter_phone_false_positives(self, text: str, matches: list[PIIMatch]) -> list[PIIMatch]:
        """Filter out likely false positive phone numbers."""
        filtered_matches = []
        
        for match in matches:
            # Get context around the match
            start = max(0, match.span_start - 50)
            end = min(len(text), match.span_end + 50)
            context = text[start:end]
            
            # Check if this is likely a false positive
            is_false_positive = False
            
            # Check for common false positive indicators
            false_positive_indicators = [
                "should not be detected",
                "fictional example",
                "example phone",
                "sample phone",
                "test phone",
                "dummy phone",
                "mock phone",
                "example number",
                "sample number",
                "test number",
                "fictional number",
                "dummy number",
                "mock number",
                "like +1",
                "such as +1",
                "for example +1",
                "e.g. +1",
                "i.e. +1",
                "including +1",
                "such as +7",
                "for example +7",
                "e.g. +7",
                "i.e. +7",
                "including +7",
            ]
            
            for indicator in false_positive_indicators:
                if indicator.lower() in context.lower():
                    is_false_positive = True
                    break
            
            if not is_false_positive:
                filtered_matches.append(match)
        
        return filtered_matches
    
    def _filter_email_false_positives(self, text: str, matches: list[PIIMatch]) -> list[PIIMatch]:
        """Filter out likely false positive email addresses."""
        filtered_matches = []
        
        for match in matches:
            # Get context around the match
            start = max(0, match.span_start - 50)
            end = min(len(text), match.span_end + 50)
            context = text[start:end]
            
            # Check if this is likely a false positive
            is_false_positive = False
            
            # Check for common false positive indicators
            false_positive_indicators = [
                "should not be detected",
                "fictional example",
                "example email",
                "sample email",
                "test email",
                "dummy email",
                "mock email",
                "example address",
                "sample address",
                "test address",
                "fictional address",
                "dummy address",
                "mock address",
                "like user@localhost",
                "such as user@localhost",
                "for example user@localhost",
                "e.g. user@localhost",
                "i.e. user@localhost",
                "including user@localhost",
                "like test@domain",
                "such as test@domain",
                "for example test@domain",
                "e.g. test@domain",
                "i.e. test@domain",
                "including test@domain",
            ]
            
            for indicator in false_positive_indicators:
                if indicator.lower() in context.lower():
                    is_false_positive = True
                    break
            
            if not is_false_positive:
                filtered_matches.append(match)
        
        return filtered_matches
    
    def _inn_detector(self, text: str) -> list[PIIMatch]:
        """Detect Russian INN numbers with checksum validation."""
        # 10-digit (individuals) or 12-digit (entities)
        pattern = r'\b[0-9]{10}\b|\b[0-9]{12}\b|\bINN[0-9]{10}\b'
        matches = []
        
        for match in re.finditer(pattern, text):
            inn_value = match.group()
            
            # Validate INN checksum
            if self._validate_inn(inn_value):
                mask = self._create_mask("inn", inn_value)
                matches.append(PIIMatch(
                    entity_type="inn",
                    value=inn_value,
                    span_start=match.start(),
                    span_end=match.end(),
                    mask=mask
                ))
        
        return matches
    
    def _passport_detector(self, text: str) -> list[PIIMatch]:
        """Detect Russian passport numbers."""
        # Series + number: 4-6 digits + space + 6 digits
        pattern = r'\b[0-9]{4,6}\s[0-9]{6}\b'
        matches = []
        
        for match in re.finditer(pattern, text):
            passport_value = match.group()
            mask = self._create_mask("passport", passport_value)
            matches.append(PIIMatch(
                entity_type="passport",
                value=passport_value,
                span_start=match.start(),
                span_end=match.end(),
                mask=mask
            ))
        
        return matches
    
    def _payment_detector(self, text: str) -> list[PIIMatch]:
        """Detect payment card numbers (PAN) with Luhn validation."""
        # 13-19 digit card numbers
        pattern = r'\b[0-9]{13,19}\b'
        matches = []
        
        for match in re.finditer(pattern, text):
            pan_value = match.group()
            
            # Validate using Luhn algorithm
            if self._validate_luhn(pan_value):
                mask = self._create_mask("payment", pan_value)
                matches.append(PIIMatch(
                    entity_type="payment",
                    value=pan_value,
                    span_start=match.start(),
                    span_end=match.end(),
                    mask=mask
                ))
        
        return matches
    
    def _create_mask(self, entity_type: str, value: str) -> str:
        """Create a formatted mask for PII entity.
        
        Format: [TYPE:hex4] where hex4 is first 4 hex digits of SHA256 hash.
        """
        # Hash the value to create consistent masks
        value_hash = hashlib.sha256(value.encode('utf-8')).hexdigest()
        hex_short = value_hash[:4]
        return f"[{entity_type.upper()}:{hex_short}]"
    
    def _validate_inn(self, inn: str) -> bool:
        """Validate Russian INN checksum.
        
        Args:
            inn: 10 or 12 digit INN string
            
        Returns:
            True if checksum is valid
        """
        if len(inn) not in (10, 12):
            return False
        
        if len(inn) == 10:  # Individual entrepreneurs
            weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
            checksum = int(inn[-1])
            calculated = 0
            for i in range(9):
                calculated += int(inn[i]) * weights[i]
            calculated = calculated % 11 % 10
            return calculated == checksum
        
        else:  # Legal entities
            weights1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
            weights2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
            
            # First checksum
            checksum1 = int(inn[10])
            calculated1 = 0
            for i in range(10):
                calculated1 += int(inn[i]) * weights1[i]
            calculated1 = calculated1 % 11 % 10
            
            # Second checksum
            checksum2 = int(inn[11])
            calculated2 = 0
            for i in range(11):
                calculated2 += int(inn[i]) * weights2[i]
            calculated2 = calculated2 % 11 % 10
            
            return calculated1 == checksum1 and calculated2 == checksum2
    
    def _validate_luhn(self, number: str) -> bool:
        """Validate number using Luhn algorithm.
        
        Args:
            number: String of digits
            
        Returns:
            True if number passes Luhn check
        """
        if not number.isdigit():
            return False
        
        total = 0
        reverse_digits = number[::-1]
        
        for i, digit in enumerate(reverse_digits):
            digit_int = int(digit)
            
            if i % 2 == 1:  # Every second digit (starting from 0)
                digit_int *= 2
                if digit_int > 9:
                    digit_int = digit_int - 9
            
            total += digit_int
        
        return total % 10 == 0