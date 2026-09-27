"""Debug +44 phone format character by character."""

import re

text = "International: +1 555-123-4567 or +44 20 7946 0958"

print(f"Text: {text}")
print(f"Length: {len(text)}")

# Print character by character around the +44 part
for i, char in enumerate(text):
    if '+44' in text[i:i+10]:
        print(f"\nFound +44 at position {i}")
        print(f"Context: ...{text[max(0,i-5):i+15]}...")
        break

# Test the exact substring
substring = "+44 20 7946 0958"
print(f"\nLooking for: '{substring}'")
print(f"Found in text: {substring in text}")

# Test with just the number
number_part = "20 7946 0958"
print(f"Looking for: '{number_part}'")
print(f"Found in text: {number_part in text}")

# Test different regex patterns
patterns = [
    r'\+44\s?[0-9]{4}[-\s]?[0-9]{6}',
    r'\+44\s\d{4}\s\d{6}',
    r'\+44\s\d{4}-\d{6}',
]

for pattern in patterns:
    matches = re.findall(pattern, text)
    print(f"\nPattern '{pattern}': {matches}")