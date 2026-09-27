"""Field-aware PII masking with configurable policies per field type.

PC14 — [T2.2.1] FieldMasker с конфигом по типам полей (YAML)
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from agent_obs.guardrail.pii_types import PIIMatch

logger = logging.getLogger(__name__)


class FieldMasker:
    """Apply PII masking policies based on field type from YAML config.
    
    Supports different masking strategies per field type:
    - no_mask: Skip PII detection entirely (even if entities found)
    - full_mask: Mask all PII entities with [TYPE:hex4] tokens
    - partial_mask: Only mask specific PII fields (enhanced in PC15 for SQL/JSON)
    - hash_only: Replace entire text with sha256 hash + char count
    
    Parameters
    ----------
    config : dict
        Dictionary with 'policies' mapping field names to policy names,
        and optional 'default_policy' for unknown fields.
    """
    
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._policies = config.get("policies", {})
        self._default_policy = config.get("default_policy", "full_mask")
        
        # Validate configuration
        valid_policies = {"no_mask", "full_mask", "partial_mask", "hash_only"}
        for policy_name in self._policies.values():
            if policy_name not in valid_policies:
                raise ValueError(f"Invalid policy '{policy_name}'. Must be one of: {valid_policies}")
        if self._default_policy not in valid_policies:
            raise ValueError(f"Invalid default_policy '{self._default_policy}'. Must be one of: {valid_policies}")
    
    @classmethod
    def from_yaml(cls, yaml_path: str | Path) -> FieldMasker:
        """Load configuration from YAML file.
        
        Parameters
        ----------
        yaml_path : str or Path
            Path to YAML configuration file.
            
        Returns
        -------
        FieldMasker
            Instance loaded from YAML config.
        """
        with open(yaml_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return cls(config)
    
    def get_policy(self, field_name: str) -> str:
        """Get masking policy for a field name.
        
        Parameters
        ----------
        field_name : str
            Field name (e.g., "system_prompt", "user_message").
            
        Returns
        -------
        str
            Policy name ("no_mask", "full_mask", "partial_mask", "hash_only").
        """
        return self._policies.get(field_name, self._default_policy)
    
    def apply(
        self, 
        field_name: str, 
        text: str, 
        pii_matches: list[PIIMatch]
    ) -> tuple[str, list[str]]:
        """Apply masking policy to text with detected PII entities.
        
        Parameters
        ----------
        field_name : str
            Field name to determine policy.
        text : str
            Original text to mask.
        pii_matches : list[PIIMatch]
            List of detected PII entities.
            
        Returns
        -------
        tuple[str, list[str]]
            - masked_text: Text after applying masking policy
            - redacted_fields: List of redacted field paths in format "field.entity.hex4"
        """
        policy = self.get_policy(field_name)
        
        if policy == "no_mask":
            # Skip PII detection entirely - return original text
            return text, []
        
        elif policy == "hash_only":
            # Replace entire text with hash + char count
            text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
            masked_text = f"[HASH:{text_hash[:16]}]({len(text)} chars)"
            return masked_text, []
        
        elif policy == "partial_mask":
            # For PC14 - fallback to full_mask (PC15 will implement SQL/JSON parsing)
            logger.debug("partial_mask policy for field '%s' falling back to full_mask", field_name)
            return self._apply_full_mask(text, pii_matches, field_name)
        
        elif policy == "full_mask":
            # Standard full PII masking
            return self._apply_full_mask(text, pii_matches, field_name)
        
        else:
            # Should not happen due to validation in __init__
            raise ValueError(f"Unknown policy '{policy}' for field '{field_name}'")
    
    def _apply_full_mask(
        self, 
        text: str, 
        matches: list[PIIMatch], 
        field_name: str
    ) -> tuple[str, list[str]]:
        """Apply full PII masking - replace each match with its mask.
        
        Sorts matches by span_start descending to avoid index shifts,
        then replaces each match with its mask token.
        Builds redacted_fields paths like "field.entity.hex4".
        
        Parameters
        ----------
        text : str
            Original text.
        matches : list[PIIMatch]
            Detected PII entities.
        field_name : str
            Field name for building redacted_fields paths.
            
        Returns
        -------
        tuple[str, list[str]]
            - masked_text: Text with PII replaced by masks
            - redacted_fields: List of redacted field paths
        """
        if not matches:
            return text, []
        
        # Sort by span_start descending so replacements don't shift indices
        sorted_matches = sorted(matches, key=lambda m: m.span_start, reverse=True)
        masked = text
        redacted: list[str] = []
        
        for match in sorted_matches:
            masked = masked[: match.span_start] + match.mask + masked[match.span_end :]
            # Build path like "field_name.entity_type.hex4"
            hex4 = match.mask.split(":")[-1].rstrip("]") if ":" in match.mask else ""
            path = f"{field_name}.{match.entity_type}.{hex4}"
            redacted.append(path)
        
        # redacted_fields should be in forward order (original text order)
        redacted.reverse()
        return masked, redacted