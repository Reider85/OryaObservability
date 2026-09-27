"""Check the exact text that's causing false positives."""

neutral_text = """
The quick brown fox jumps over the lazy dog. This is a sample text
containing various words and phrases. It includes numbers like 12345
and combinations that might look like data but are not actual PII.
Email addresses like user@localhost or test@domain are examples.
Phone numbers in fictional contexts like +1 (555) 123-4567 should
not be detected if they are clearly fictional examples.
"""

print("=== Neutral Text Analysis ===")
print(f"Text length: {len(neutral_text)}")
print(f"Text: {repr(neutral_text)}")

# Find the exact position of the phone number
phone_number = "+1 (555) 123-4567"
pos = neutral_text.find(phone_number)
print(f"Phone number position: {pos}")
print(f"Context: {repr(neutral_text[pos-30:pos+len(phone_number)+30])}")