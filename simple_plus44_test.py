"""Simple test of +44 pattern."""

import re

def simple_test():
    text = "+44 20 7946 0958"
    pattern = r'\+44\s?[0-9]{4}[-\s]?[0-9]{6}'
    
    print(f"Text: '{text}'")
    print(f"Pattern: '{pattern}'")
    
    matches = re.findall(pattern, text)
    print(f"Matches: {matches}")
    
    # Try character by character
    print(f"Text length: {len(text)}")
    for i, char in enumerate(text):
        print(f"  {i}: '{char}'")

if __name__ == "__main__":
    simple_test()