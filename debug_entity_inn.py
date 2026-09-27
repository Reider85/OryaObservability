"""Debug entity INN validation."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_entity_inn():
    print("=== Debug Entity INN ===")
    
    detector = PIIDetector()
    
    entity_inn = "111111111113"
    
    print(f"Entity INN: {entity_inn}")
    print(f"Length: {len(entity_inn)}")
    
    # Test validation
    is_valid = detector._validate_inn(entity_inn)
    print(f"Validation: {is_valid}")
    
    # Test regex
    import re
    pattern = r'\b[0-9]{10}\b|\b[0-9]{12}\b'
    regex_matches = re.findall(pattern, entity_inn)
    print(f"Regex matches: {regex_matches}")
    
    # Test _inn_detector directly
    text = f"INN: {entity_inn}"
    inn_matches = detector._inn_detector(text)
    print(f"_inn_detector: {len(inn_matches)} matches")
    
    if inn_matches:
        print(f"  Match: {inn_matches[0].value}")
    
    # Manual validation check
    weights1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    weights2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    
    digits = [int(d) for d in entity_inn]
    print(f"Digits: {digits}")
    
    # First checksum
    checksum1 = int(digits[10])
    calculated1 = 0
    for i in range(10):
        calculated1 += digits[i] * weights1[i]
    calculated1 = calculated1 % 11 % 10
    
    # Second checksum  
    checksum2 = int(digits[11])
    calculated2 = 0
    for i in range(11):
        calculated2 += digits[i] * weights2[i]
    calculated2 = calculated2 % 11 % 10
    
    print(f"First checksum: calculated={calculated1}, actual={checksum1}, valid={calculated1 == checksum1}")
    print(f"Second checksum: calculated={calculated2}, actual={checksum2}, valid={calculated2 == checksum2}")
    print(f"Overall valid: {calculated1 == checksum1 and calculated2 == checksum2}")

if __name__ == "__main__":
    debug_entity_inn()