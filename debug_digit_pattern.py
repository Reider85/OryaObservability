"""Debug the digit pattern."""

import re

def debug_digit_pattern():
    text = "+44 20 7946 0958"
    
    # Test the exact string
    print("=== Testing exact string ===")
    exact_part = text[3:]  # "+44 20 7946 0958"[3:] = " 20 7946 0958"
    print(f"Exact part: '{exact_part}'")
    
    # Test if it matches our pattern
    pattern = r'\s[0-9]{4}'
    matches = re.findall(pattern, exact_part)
    print(f"Pattern '{pattern}' in exact_part: {matches}")
    
    # Test character by character
    print("\n=== Character by character ===")
    for i, char in enumerate(exact_part):
        print(f"  {i}: '{char}' (ord: {ord(char)})")
    
    # Test with different whitespace patterns
    print("\n=== Testing whitespace ===")
    patterns = [
        r'\s[0-9]{4}',
        r'\s[0-9 ]{4}',
        r'\s\d{4}',
        r' [0-9]{4}',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, exact_part)
        print(f"Pattern '{pattern}': {matches}")

if __name__ == "__main__":
    debug_digit_pattern()