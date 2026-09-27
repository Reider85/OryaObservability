"""Debug payment vs INN detection order."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_detection_order():
    print("=== Debug Detection Order ===")
    
    detector = PIIDetector()
    
    # Test the problematic value
    value = "5077466207712"
    text = f"INN: {value}"
    
    print(f"Testing: '{text}'")
    
    # Test individual detectors
    email_matches = detector._email_detector(text)
    phone_matches = detector._phone_detector(text)
    inn_matches = detector._inn_detector(text)
    passport_matches = detector._passport_detector(text)
    payment_matches = detector._payment_detector(text)
    
    print(f"Email matches: {len(email_matches)}")
    print(f"Phone matches: {len(phone_matches)}")
    print(f"INN matches: {len(inn_matches)}")
    print(f"Passport matches: {len(passport_matches)}")
    print(f"Payment matches: {len(payment_matches)}")
    
    # Check payment regex specifically
    import re
    payment_patterns = [
        r'\b4[0-9]{12,15}\b',  # Visa
        r'\b5[1-5][0-9]{14}\b',  # Mastercard
        r'\b3[47][0-9]{13}\b',  # Amex
        r'\b3[0-9]{13,16}\b',  # JCB
        r'\b6011[0-9]{12,15}\b',  # Discover
        r'\b65[0-9]{14,16}\b',  # Discover
        r'\b35[0-9]{14,16}\b',  # JCB
    ]
    
    for pattern in payment_patterns:
        matches = re.findall(pattern, value)
        if matches:
            print(f"Payment pattern '{pattern}' matches: {matches}")

if __name__ == "__main__":
    debug_detection_order()