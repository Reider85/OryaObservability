"""Debug +44 phone format."""

import re

def test_plus44():
    text = "International: +1 555-123-4567 or +44 20 7946 0958"
    pattern = r'\+44\s?[0-9]{4}[-\s]?[0-9]{6}'
    
    print(f"Text: {text}")
    print(f"Pattern: {pattern}")
    
    matches = re.findall(pattern, text)
    print(f"Matches: {matches}")
    
    for match in re.finditer(pattern, text):
        print(f"Match: '{match.group()}' at {match.start()}-{match.end()}")

if __name__ == "__main__":
    test_plus44()