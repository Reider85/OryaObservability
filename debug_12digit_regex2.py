"""Debug regex for 12-digit INN."""

import re

def debug_12digit_regex():
    test_inns = [
        "111111111111",  # 12 digits
        "123456789012",  # 12 digits
    ]
    
    pattern = r'\b[0-9]{10}\b|\b[0-9]{12}\b'
    
    print("=== Testing 12-digit regex ===")
    for inn in test_inns:
        print(f"\nINN: {inn}")
        print(f"Length: {len(inn)}")
        
        matches = re.findall(pattern, inn)
        print(f"Regex matches: {matches}")
        
        # Test in text
        text = f"INN: {inn}"
        matches_in_text = re.findall(pattern, text)
        print(f"In text '{text}': {matches_in_text}")

if __name__ == "__main__":
    debug_12digit_regex()