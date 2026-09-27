"""Debug +44 phone format variations."""

import re

def test_plus44_variations():
    text = "International: +1 555-123-4567 or +44 20 7946 0958"
    
    # Test different patterns
    patterns = [
        r'\+44\s?[0-9]{4}[-\s]?[0-9]{6}',  # Original
        r'\+44\s[0-9]{4}\s[0-9]{6}',        # With spaces
        r'\+44\s[0-9]{4}-[0-9]{6}',         # With dash
        r'\+44\s[0-9]{4}[0-9]{6}',          # No separator
        r'\+44[0-9]{10}',                    # No space
    ]
    
    for i, pattern in enumerate(patterns):
        print(f"\n=== Pattern {i+1}: {pattern} ===")
        matches = re.findall(pattern, text)
        print(f"Matches: {matches}")
        
        for match in re.finditer(pattern, text):
            print(f"Match: '{match.group()}' at {match.start()}-{match.end()}")

if __name__ == "__main__":
    test_plus44_variations()