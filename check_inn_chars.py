"""Check characters in problematic INNs."""

def check_characters():
    test_cases = [
        "5077466207712",  # Original test case
        "7704781060168",  # Original test case
    ]
    
    print("=== Character analysis ===")
    for case in test_cases:
        print(f"\nINN: {case}")
        print(f"Length: {len(case)}")
        print("Characters:")
        for i, char in enumerate(case):
            print(f"  {i}: '{char}' (ord: {ord(char)})")
        
        # Check if it contains only digits
        if case.isdigit():
            print("  [OK] Only digits")
        else:
            print("  [ERROR] Contains non-digits")
        
        # Test regex step by step
        import re
        pattern = r'\b[0-9]{12}\b'
        matches = re.findall(pattern, case)
        print(f"  Regex match: {matches}")

if __name__ == "__main__":
    check_characters()