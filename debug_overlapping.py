"""Debug overlapping phone patterns."""

import re

def debug_overlapping():
    text = "International: +1 555-123-4567 or +44 20 7946 0958"
    
    # Test individual patterns
    patterns = [
        (r'\+1\s?[0-9]{3}[-\s]?[0-9]{3}[-\s]?[0-9]{4}', "US format"),
        (r'\+44\s[0-9]{2}\s[0-9]{4}\s[0-9]{2}', "UK format"),
    ]
    
    print("=== Individual patterns ===")
    for pattern, desc in patterns:
        matches = re.findall(pattern, text)
        print(f"{desc}: {matches}")
        
        for match in re.finditer(pattern, text):
            print(f"  Match: '{match.group()}' at {match.start()}-{match.end()}")
    
    # Test the combined approach used in PIIDetector
    print("\n=== Combined approach ===")
    all_patterns = [
        r'\+1\s?[0-9]{3}[-\s]?[0-9]{3}[-\s]?[0-9]{4}',  # US format
        r'\+44\s[0-9]{2}\s[0-9]{4}\s[0-9]{2}',          # UK format
    ]
    
    # Simulate the PIIDetector approach
    for pattern_str in all_patterns:
        pattern = pattern_str.split('#')[0].strip()
        for match in re.finditer(pattern, text):
            print(f"Pattern '{pattern}': '{match.group()}' at {match.start()}-{match.end()}")

if __name__ == "__main__":
    debug_overlapping()