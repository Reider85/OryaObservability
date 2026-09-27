"""Field-aware PII masking with configurable policies per field type.

PC14 — [T2.2.1] FieldMasker с конфигом по типам полей (YAML)
PC15 — [T2.2.2] Парсинг tool_output: SQL-колонки и REST JSON-path
"""

from __future__ import annotations

import hashlib
import json
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
    
    def __init__(self, config: dict[str, Any], pii_columns_config: dict[str, list[str]] | None = None) -> None:
        self.config = config
        self._policies = config.get("policies", {})
        self._default_policy = config.get("default_policy", "full_mask")
        
        # PII column configuration for structured data parsing
        self._pii_columns_config = pii_columns_config or {}
        self._column_to_pii_type = self._build_column_to_pii_type_mapping()
        
        # Validate configuration
        valid_policies = {"no_mask", "full_mask", "partial_mask", "hash_only"}
        for policy_name in self._policies.values():
            if policy_name not in valid_policies:
                raise ValueError(f"Invalid policy '{policy_name}'. Must be one of: {valid_policies}")
        if self._default_policy not in valid_policies:
            raise ValueError(f"Invalid default_policy '{self._default_policy}'. Must be one of: {valid_policies}")
    
    @classmethod
    def from_yaml(cls, yaml_path: str | Path, pii_columns_yaml_path: str | Path | None = None) -> FieldMasker:
        """Load configuration from YAML file.
        
        Parameters
        ----------
        yaml_path : str or Path
            Path to YAML configuration file.
        pii_columns_yaml_path : str or Path, optional
            Path to PII columns YAML configuration file. If None, no structured parsing.
            
        Returns
        -------
        FieldMasker
            Instance loaded from YAML config.
        """
        with open(yaml_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        # Load PII columns config if provided
        pii_columns_config = None
        if pii_columns_yaml_path:
            with open(pii_columns_yaml_path, "r", encoding="utf-8") as f:
                pii_columns_config = yaml.safe_load(f)
        
        return cls(config, pii_columns_config)
    
    def _build_column_to_pii_type_mapping(self) -> dict[str, str]:
        """Build reverse mapping from column names to PII types.
        
        Returns
        -------
        dict[str, str]
            Mapping from lowercase column name to PII type.
        """
        column_to_pii = {}
        for pii_type, column_names in self._pii_columns_config.items():
            for column_name in column_names:
                column_to_pii[column_name.lower()] = pii_type
                # Also add uppercase and titlecase versions
                column_to_pii[column_name.upper()] = pii_type
                column_to_pii[column_name.title()] = pii_type
        return column_to_pii
    
    def _is_sql_result(self, text: str) -> bool:
        """Check if text looks like a SQL result set.
        
        Parameters
        ----------
        text : str
            Text to check.
            
        Returns
        -------
        bool
            True if text appears to be SQL result JSON format.
        """
        try:
            data = json.loads(text)
            return isinstance(data, dict) and "columns" in data and "rows" in data
        except (json.JSONDecodeError, TypeError):
            return False
    
    def _parse_sql_result(self, text: str) -> tuple[list[str], list[dict]]:
        """Parse SQL result JSON format into columns and rows.
        
        Parameters
        ----------
        text : str
            JSON text in SQL result format: {"columns": [...], "rows": [...]}
            
        Returns
        -------
        tuple[list[str], list[dict]]
            - List of column names
            - List of row dictionaries
        """
        data = json.loads(text)
        columns = data["columns"]
        rows = data["rows"]
        return columns, rows
    
    def _is_json(self, text: str) -> bool:
        """Check if text is valid JSON.
        
        Parameters
        ----------
        text : str
            Text to check.
            
        Returns
        -------
        bool
            True if text is valid JSON.
        """
        try:
            json.loads(text)
            return True
        except (json.JSONDecodeError, TypeError):
            return False
    
    def _apply_partial_mask_sql(
        self, 
        text: str, 
        matches: list[PIIMatch], 
        field_name: str
    ) -> tuple[str, list[str]]:
        """Apply partial masking to SQL result - mask only PII columns.
        
        Parameters
        ----------
        text : str
            Original SQL result JSON text.
        matches : list[PIIMatch]
            PII matches detected in the original text.
        field_name : str
            Field name for building redacted_fields paths.
            
        Returns
        -------
        tuple[str, list[str]]
            - masked_text: JSON text with only PII columns masked
            - redacted_fields: List of redacted field paths
        """
        try:
            columns, rows = self._parse_sql_result(text)
            masked_rows = []
            redacted_fields = []
            
            # Find which columns contain PII
            pii_columns = []
            for col in columns:
                if col.lower() in self._column_to_pii_type:
                    pii_columns.append(col)
            
            if not pii_columns:
                # No PII columns found - return unchanged
                return text, []
            
            # Create a mapping of PII types to their masks
            pii_masks = {}
            for match in matches:
                pii_masks[match.value] = match.mask
            
            # Process each row
            for row_idx, row in enumerate(rows):
                masked_row = {}
                for col in columns:
                    col_value = str(row.get(col, ""))
                    
                    if col in pii_columns:
                        # Find PII values in this column
                        matched_values = []
                        for match_value, mask in pii_masks.items():
                            if match_value in col_value:
                                matched_values.append(match_value)
                        
                        if matched_values:
                            # Apply masking to this column value
                            masked_value = col_value
                            for matched_value in matched_values:
                                masked_value = masked_value.replace(matched_value, pii_masks[matched_value])
                            
                            masked_row[col] = masked_value
                            
                            # Add redacted fields
                            for matched_value in matched_values:
                                mask = pii_masks[matched_value]
                                hex4 = mask.split(":")[-1].rstrip("]") if ":" in mask else ""
                                path = f"{field_name}.rows[{row_idx}].{col}.{hex4}"
                                redacted_fields.append(path)
                        else:
                            masked_row[col] = col_value
                    else:
                        # Non-PII columns - leave unchanged
                        masked_row[col] = col_value
                
                masked_rows.append(masked_row)
            
            # Rebuild JSON
            result = {"columns": columns, "rows": masked_rows}
            masked_text = json.dumps(result, ensure_ascii=False, indent=2)
            
            return masked_text, redacted_fields
            
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Fallback to full mask if parsing fails
            logger.warning("Failed to parse SQL result, falling back to full mask")
            return self._apply_full_mask(text, matches, field_name)
    
    def _find_column_start(self, text: str, row_idx: int, col_idx: int) -> int:
        """Find the start position of a specific column in the original text.
        
        This is a heuristic approach - we need to find where each column starts
        in the original JSON text to correctly map PII matches to columns.
        """
        try:
            data = json.loads(text)
            rows = data["rows"]
            
            if row_idx >= len(rows):
                return 0
            
            row = rows[row_idx]
            columns = data["columns"]
            
            # Build a string representation of the row and find the column
            row_str = str(row)
            col_value = str(row.get(columns[col_idx], ""))
            
            # Find where this column value appears in the row string
            pos = row_str.find(col_value)
            if pos == -1:
                return 0
            
            # Estimate the position in the original text
            # This is approximate but good enough for our purposes
            return pos
            
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return 0
    
    def _find_column_end(self, text: str, row_idx: int, col_idx: int) -> int:
        """Find the end position of a specific column in the original text."""
        start = self._find_column_start(text, row_idx, col_idx)
        try:
            data = json.loads(text)
            rows = data["rows"]
            
            if row_idx >= len(rows):
                return start
            
            row = rows[row_idx]
            columns = data["columns"]
            
            col_value = str(row.get(columns[col_idx], ""))
            return start + len(col_value)
            
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return start
    
    def _apply_full_mask_to_string(self, text: str, matches: list[PIIMatch]) -> str:
        """Apply full masking to a single string with matches."""
        if not matches:
            return text
        
        # Sort by span_start descending to avoid index shifts
        sorted_matches = sorted(matches, key=lambda m: m.span_start, reverse=True)
        masked = text
        
        for match in sorted_matches:
            masked = masked[: match.span_start] + match.mask + masked[match.span_end :]
        
        return masked
    
    def _apply_partial_mask_json(
        self, 
        text: str, 
        matches: list[PIIMatch], 
        field_name: str
    ) -> tuple[str, list[str]]:
        """Apply partial masking to JSON - mask only PII fields.
        
        Parameters
        ----------
        text : str
            Original JSON text.
        matches : list[PIIMatch]
            PII matches detected in the original text.
        field_name : str
            Field name for building redacted_fields paths.
            
        Returns
        -------
        tuple[str, list[str]]
            - masked_text: JSON text with only PII fields masked
            - redacted_fields: List of redacted field paths
        """
        try:
            data = json.loads(text)
            masked_data, redacted_fields = self._mask_json_recursive(
                data, matches, field_name, "$"
            )
            masked_text = json.dumps(masked_data, ensure_ascii=False, indent=2)
            return masked_text, redacted_fields
        except (json.JSONDecodeError, TypeError, ValueError):
            # Fallback to full mask if parsing fails
            logger.warning("Failed to parse JSON, falling back to full mask")
            return self._apply_full_mask(text, matches, field_name)
    
    def _mask_json_recursive(
        self, 
        data: Any, 
        matches: list[PIIMatch], 
        field_name: str, 
        path: str
    ) -> tuple[Any, list[str]]:
        """Recursively mask JSON data, only masking PII fields.
        
        Parameters
        ----------
        data : Any
            JSON data (dict, list, or primitive)
        matches : list[PIIMatch]
            PII matches detected in the original text.
        field_name : str
            Field name for building redacted_fields paths.
        path : str
            Current JSON path (e.g., "$", "$.user", "$.users[0]")
            
        Returns
        -------
        tuple[Any, list[str]]
            - masked_data: Masked JSON data
            - redacted_fields: List of redacted field paths
        """
        if isinstance(data, dict):
            masked_dict = {}
            redacted_fields = []
            
            for key, value in data.items():
                current_path = f"{path}.{key}" if path != "$" else f"${{{key}}}"
                
                # Check if this key matches a PII column name
                if key.lower() in self._column_to_pii_type:
                    # Mask this field completely if it contains any PII
                    str_value = str(value)
                    masked_value = str_value
                    
                    # Find matches that are exactly in this value
                    for match in matches:
                        if match.value in str_value:
                            masked_value = str_value.replace(match.value, match.mask)
                            # Add redacted fields
                            hex4 = match.mask.split(":")[-1].rstrip("]") if ":" in match.mask else ""
                            field_path = f"{field_name}.{current_path}.{match.entity_type}.{hex4}"
                            redacted_fields.append(field_path)
                    
                    masked_dict[key] = masked_value
                else:
                    # Non-PII field - recurse
                    masked_value, field_redactions = self._mask_json_recursive(
                        value, matches, field_name, current_path
                    )
                    masked_dict[key] = masked_value
                    redacted_fields.extend(field_redactions)
            
            return masked_dict, redacted_fields
        
        elif isinstance(data, list):
            masked_list = []
            redacted_fields = []
            
            for i, item in enumerate(data):
                current_path = f"{path}[{i}]"
                
                # Recurse into list items
                masked_item, field_redactions = self._mask_json_recursive(
                    item, matches, field_name, current_path
                )
                masked_list.append(masked_item)
                redacted_fields.extend(field_redactions)
            
            return masked_list, redacted_fields
        
        else:
            # Primitive value - no masking needed
            return data, []
    
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
            # PC15: Parse structured data (SQL/JSON) and mask only PII fields
            if self._pii_columns_config:
                if self._is_sql_result(text):
                    logger.debug("Applying partial_mask to SQL result for field '%s'", field_name)
                    return self._apply_partial_mask_sql(text, pii_matches, field_name)
                elif self._is_json(text):
                    logger.debug("Applying partial_mask to JSON for field '%s'", field_name)
                    return self._apply_partial_mask_json(text, pii_matches, field_name)
                else:
                    logger.debug("Field '%s' is not structured data, falling back to full_mask", field_name)
                    return self._apply_full_mask(text, pii_matches, field_name)
            else:
                # No PII columns config - fall back to full mask
                logger.debug("No PII columns config, falling back to full_mask for field '%s'", field_name)
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