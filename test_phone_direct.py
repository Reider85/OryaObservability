"""Quick test of phone regex directly."""

import re

def test_phone_direct():
    text = "Call +7 (999) 123-45-67 or 89001234567"
    pattern = r'\b(\+7\s*\(?\s*[0-9]{3}\s*\)?\s*[0-9]{3}[-\s]?[0-9]{2}[-\s]?[0-9]{2}|8[0-9]{10})\b'
    
    matches = re.findall(pattern, text)
    print(f"All matches: {matches}")
    
    for match in re.finditer(pattern, text):
        print(f"Match: '{match.group()}' at {match.start()}-{match.end()}")

if __name__ == "__main__":
    test_phone_direct()