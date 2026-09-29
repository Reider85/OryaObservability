import json
from agent_obs.guardrail.field_masker import FieldMasker
from agent_obs.guardrail.pii_types import PIIMatch

# Test JSON nested PII
config = {'policies': {'tool_output': 'partial_mask'}}
pii_columns_config = {'email': ['email', 'contact_email'], 'phone': ['phone', 'contact_phone']}
masker = FieldMasker(config, pii_columns_config)

json_data = {
    'user': {
        'id': 1,
        'name': 'Alice',
        'email': 'alice@example.com',
        'contact_email': 'alice.work@example.com',
        'profile': {
            'phone': '+1234567890',
            'contact_phone': '+9876543210'
        }
    },
    'metadata': {
        'timestamp': '2023-01-01',
        'version': '1.0'
    }
}
json_text = json.dumps(json_data)

print('Original JSON:')
print(json_text)
print()

# Find positions
email_pos = json_text.find('alice@example.com')
contact_email_pos = json_text.find('alice.work@example.com')
phone_pos = json_text.find('+1234567890')
contact_phone_pos = json_text.find('+9876543210')

print(f'Email position: {email_pos}-{email_pos + len("alice@example.com")}')
print(f'Contact email position: {contact_email_pos}-{contact_email_pos + len("alice.work@example.com")}')
print(f'Phone position: {phone_pos}-{phone_pos + len("+1234567890")}')
print(f'Contact phone position: {contact_phone_pos}-{contact_phone_pos + len("+9876543210")}')
print()

pii_matches = [
    PIIMatch('email', 'alice@example.com', email_pos, email_pos + len('alice@example.com'), '[EMAIL:5f3a]'),
    PIIMatch('email', 'alice.work@example.com', contact_email_pos, contact_email_pos + len('alice.work@example.com'), '[EMAIL:abc1]'),
    PIIMatch('phone', '+1234567890', phone_pos, phone_pos + len('+1234567890'), '[PHONE:def2]'),
    PIIMatch('phone', '+9876543210', contact_phone_pos, contact_phone_pos + len('+9876543210'), '[PHONE:ghi3]'),
]

masked_text, redacted_fields = masker.apply('tool_output', json_text, pii_matches)
print('Masked JSON:')
print(masked_text)
print()
print('Redacted fields:', redacted_fields)