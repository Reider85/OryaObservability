"""Debug duplicate phone detection."""

import re

def debug_duplicate():
    text = "No spaces: +79991234567"
    
    # Test patterns that might overlap
    patterns = [
        (r'\+7\s*\(?\s*[0-9]{3}\s*\)?\s*[0-9]{3}[-\s]?[0-9]{2}[-\s]?[0-9]{2}', "Complex +7 format"),
        (r'\+7[0-9]{10}', "Simple +7 format"),
        (r'\b8[0-9]{10}\b', "8 format"),
    ]
    
    print("=== Testing patterns on '+79991234567' ===")
    for pattern, desc in patterns:
        matches = re.findall(pattern, text)
        print(f"{desc}: {matches}")
        
        for match in re.finditer(pattern, text):
            print(f"  Match: '{match.group()}' at {match.start()}-{match.end()}")
    
    # Test the issue: +79991234567 matches both complex and simple patterns
    print("\n=== Testing overlap issue ===")
    text1 = "+79991234567"
    
    for pattern, desc in patterns:
        if pattern != r'\b8[0-9]{10}\b':  # Skip 8 format for this test
            matches = re.findall(pattern, text1)
            print(f"{desc} on '{text1}': {matches}")

if __name__ == "__main__":
    debug_duplicate()