"""Debug INN regex pattern."""

import re

def debug_inn_regex():
    text = "INN: 0276453431"
    pattern = r'\b[0-9]{10}\b|\b[0-9]{12}\b'
    
    print(f"Text: {text}")
    print(f"Pattern: {pattern}")
    
    matches = re.findall(pattern, text)
    print(f"Regex matches: {matches}")
    
    for match in re.finditer(pattern, text):
        print(f"Match: '{match.group()}' at {match.start()}-{match.end()}")
    
    # Test individual components
    print("\n=== Testing individual INN lengths ===")
    
    test_cases = [
        "0276453431",    # 10 digits
        "7707087890",    # 10 digits
        "5077466207712", # 12 digits
        "7704781060168", # 12 digits
    ]
    
    for inn in test_cases:
        matches = re.findall(pattern, inn)
        print(f"INN {inn}: {matches}")

if __name__ == "__main__":
    debug_inn_regex()