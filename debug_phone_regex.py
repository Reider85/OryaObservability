"""Debug phone regex specifically."""

import re

def test_phone_regex():
    """Test phone regex patterns."""
    text = "Call +7 (999) 123-45-67 or 89001234567"
    print(f"Text: {text}")
    
    # Original pattern
    pattern1 = r'''
        \b
        (?:
            \+7\s*[\(]?\s*[0-9]{3}\s*[\)]?\s*[0-9]{3}[-\s]?[0-9]{2}[-\s]?[0-9]{2}  # +7 (XXX) XXX-XX-XX
            | 8[0-9]{10}                                                      # 8XXXXXXXXXX
            | \+7[0-9]{10}                                                   # +7XXXXXXXXXX
            | \+1\s?[0-9]{3}[-\s]?[0-9]{3}[-\s]?[0-9]{4}                    # +1 XXX-XXX-XXXX
            | \+44\s?[0-9]{4}[-\s]?[0-9]{6}                                 # +44 XXXX XXXXXX
            | \+1\s?\([0-9]{3}\)\s?[0-9]{3}[-\s]?[0-9]{4}                   # +1 (XXX) XXX-XXXX
        )
        \b
    '''
    
    # Simplified pattern
    pattern2 = r'\+7\s*\(?\s*[0-9]{3}\s*\)?\s*[0-9]{3}[-\s]?[0-9]{2}[-\s]?[0-9]{2}|\b8[0-9]{10}\b'
    
    print("=== Pattern 1 (Original) ===")
    for match in re.finditer(pattern1, text, re.VERBOSE):
        print(f"Match: '{match.group()}' at {match.start()}-{match.end()}")
    
    print("\n=== Pattern 2 (Simplified) ===")
    for match in re.finditer(pattern2, text):
        print(f"Match: '{match.group()}' at {match.start()}-{match.end()}")
    
    # Test individual parts
    print("\n=== Individual Components ===")
    
    # Test +7 format
    sub_pattern = r'\+7\s*\(?\s*[0-9]{3}\s*\)?\s*[0-9]{3}[-\s]?[0-9]{2}[-\s]?[0-9]{2}'
    for match in re.finditer(sub_pattern, text):
        print(f"+7 format match: '{match.group()}' at {match.start()}-{match.end()}")
    
    # Test 8 format
    sub_pattern = r'\b8[0-9]{10}\b'
    for match in re.finditer(sub_pattern, text):
        print(f"8 format match: '{match.group()}' at {match.start()}-{match.end()}")

if __name__ == "__main__":
    test_phone_regex()