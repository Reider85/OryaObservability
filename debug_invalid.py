import json
from agent_obs.guardrail.field_masker import FieldMasker
from agent_obs.guardrail.pii_types import PIIMatch

# Test invalid JSON
config = {'policies': {'tool_output': 'partial_mask'}}
pii_columns_config = {'email': ['email'], 'phone': ['phone']}
masker = FieldMasker(config, pii_columns_config)

invalid_json = '{"columns": ["id", "name"], "rows": [{"id": 1, "name": "Alice"}'  # missing closing brace

print('Invalid JSON:')
print(invalid_json)
print()

# Find position of "Alice"
alice_pos = invalid_json.find("Alice")

print(f'Alice position: {alice_pos}-{alice_pos + len("Alice")}')
print()

pii_matches = [
    PIIMatch('email', 'alice@example.com', alice_pos, alice_pos + len("Alice"), '[EMAIL:5f3a]'),
]

masked_text, redacted_fields = masker.apply('tool_output', invalid_json, pii_matches)
print('Masked JSON:')
print(masked_text)
print()
print('Redacted fields:', redacted_fields)