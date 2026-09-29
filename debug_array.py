import json
from agent_obs.guardrail.field_masker import FieldMasker
from agent_obs.guardrail.pii_types import PIIMatch

# Test JSON array
config = {'policies': {'tool_output': 'partial_mask'}}
pii_columns_config = {'email': ['email'], 'phone': ['phone']}
masker = FieldMasker(config, pii_columns_config)

json_data = [
    {"id": 1, "name": "Alice", "email": "alice@example.com", "phone": "+1234567890"},
    {"id": 2, "name": "Bob", "email": "bob@test.com", "phone": "+9876543210"}
]
json_text = json.dumps(json_data)

print('Original JSON:')
print(json_text)
print()

# Find positions
email1_pos = json_text.find('alice@example.com')
phone1_pos = json_text.find('+1234567890')
email2_pos = json_text.find('bob@test.com')
phone2_pos = json_text.find('+9876543210')

print(f'Email 1 position: {email1_pos}-{email1_pos + len("alice@example.com")}')
print(f'Phone 1 position: {phone1_pos}-{phone1_pos + len("+1234567890")}')
print(f'Email 2 position: {email2_pos}-{email2_pos + len("bob@test.com")}')
print(f'Phone 2 position: {phone2_pos}-{phone2_pos + len("+9876543210")}')
print()

pii_matches = [
    PIIMatch('email', 'alice@example.com', email1_pos, email1_pos + len('alice@example.com'), '[EMAIL:5f3a]'),
    PIIMatch('phone', '+1234567890', phone1_pos, phone1_pos + len('+1234567890'), '[PHONE:def2]'),
    PIIMatch('email', 'bob@test.com', email2_pos, email2_pos + len('bob@test.com'), '[EMAIL:abc1]'),
    PIIMatch('phone', '+9876543210', phone2_pos, phone2_pos + len('+9876543210'), '[PHONE:ghi3]'),
]

masked_text, redacted_fields = masker.apply('tool_output', json_text, pii_matches)
print('Masked JSON:')
print(masked_text)
print()
print('Redacted fields:', redacted_fields)