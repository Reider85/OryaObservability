"""Test 12-digit pattern specifically."""

import re

def test_12_digit():
    # Test the 12-digit pattern with different numbers
    pattern = r'\b[0-9]{12}\b'
    
    test_cases = [
        "5077466207712",  # Original test case
        "7704781060168",  # Original test case
        "123456789012",   # Simple 12 digits
        "000000000000",   # All zeros
    ]
    
    print("=== Testing 12-digit pattern ===")
    for case in test_cases:
        matches = re.findall(pattern, case)
        print(f"'{case}': {matches}")
        
        # Test with word boundaries
        for match in re.finditer(pattern, case):
            print(f"  Match: '{match.group()}' at {match.start()}-{match.end()}")
    
    # Test without word boundaries
    print("\n=== Testing without word boundaries ===")
    pattern_no_word = r'[0-9]{12}'
    
    for case in test_cases:
        matches = re.findall(pattern_no_word, case)
        print(f"'{case}' (no word boundaries): {matches}")

if __name__ == "__main__":
    test_12_digit()