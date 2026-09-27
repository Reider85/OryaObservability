"""Debug 12-digit INN regex specifically."""

import re

def debug_12digit_regex():
    # Test the corrected 12-digit INNs
    test_inns = [
        "507746620771",  # Corrected entity INN
        "770478106016",  # Corrected entity INN
    ]
    
    pattern = r'\b[0-9]{10}\b|\b[0-9]{12}\b'
    
    print("=== Testing 12-digit regex ===")
    for inn in test_inns:
        print(f"\nINN: {inn}")
        print(f"Length: {len(inn)}")
        
        matches = re.findall(pattern, inn)
        print(f"Regex matches: {matches}")
        
        for match in re.finditer(pattern, inn):
            print(f"Match: '{match.group()}' at {match.start()}-{match.end()}")
        
        # Test in text
        text = f"INN: {inn}"
        matches_in_text = re.findall(pattern, text)
        print(f"In text '{text}': {matches_in_text}")

if __name__ == "__main__":
    debug_12digit_regex()