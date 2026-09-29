import json
from agent_obs.guardrail.field_masker import FieldMasker
from agent_obs.guardrail.pii_types import PIIMatch

# Test the actual behavior
config = {'policies': {'tool_output': 'partial_mask'}}
pii_columns_config = {'email': ['email'], 'phone': ['phone']}
masker = FieldMasker(config, pii_columns_config)

unstructured_text = 'Contact Alice at alice@example.com or call +1234567890'

# Find actual positions
email_pos = unstructured_text.find('alice@example.com')
phone_pos = unstructured_text.find('+1234567890')

print(f'Email position: {email_pos}-{email_pos + len("alice@example.com")}')
print(f'Phone position: {phone_pos}-{phone_pos + len("+1234567890")}')

pii_matches = [
    PIIMatch('email', 'alice@example.com', email_pos, email_pos + len('alice@example.com'), '[EMAIL:5f3a]'),
    PIIMatch('phone', '+1234567890', phone_pos, phone_pos + len('+1234567890'), '[PHONE:def2]'),
]

masked_text, redacted_fields = masker.apply('tool_output', unstructured_text, pii_matches)
print(f'Masked text: {masked_text}')
print(f'Redacted fields: {redacted_fields}')