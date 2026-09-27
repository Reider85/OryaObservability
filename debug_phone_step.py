"""Debug phone regex step by step."""

import re

def test_phone_step_by_step():
    text = "Call +7 (999) 123-45-67 or 89001234567"
    
    print("=== Testing +7 pattern ===")
    pattern1 = r'\+7\s*\(?\s*[0-9]{3}\s*\)?\s*[0-9]{3}[-\s]?[0-9]{2}[-\s]?[0-9]{2}'
    matches1 = re.findall(pattern1, text)
    print(f"+7 matches: {matches1}")
    
    for match in re.finditer(pattern1, text):
        print(f"+7 match: '{match.group()}' at {match.start()}-{match.end()}")
    
    print("\n=== Testing 8 pattern ===")
    pattern2 = r'\b8[0-9]{10}\b'
    matches2 = re.findall(pattern2, text)
    print(f"8 matches: {matches2}")
    
    for match in re.finditer(pattern2, text):
        print(f"8 match: '{match.group()}' at {match.start()}-{match.end()}")
    
    print("\n=== Testing combined pattern ===")
    combined = pattern1 + '|' + pattern2
    matches_combined = re.findall(combined, text)
    print(f"Combined matches: {matches_combined}")
    
    for match in re.finditer(combined, text):
        print(f"Combined match: '{match.group()}' at {match.start()}-{match.end()}")

if __name__ == "__main__":
    test_phone_step_by_step()