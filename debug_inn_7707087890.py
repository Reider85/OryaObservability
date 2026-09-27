"""Debug INN validation for 7707087890."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_inn_7707087890():
    print("=== Debug INN 7707087890 ===")
    
    detector = PIIDetector()
    
    inn = "7707087890"
    print(f"INN: {inn}")
    print(f"Length: {len(inn)}")
    
    # Test validation
    is_valid = detector._validate_inn(inn)
    print(f"Validation: {is_valid}")
    
    # Manual calculation
    weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    digits = [int(d) for d in inn]
    
    print(f"Digits: {digits}")
    print(f"Weights: {weights}")
    
    calculated = 0
    for i in range(9):
        calculated += digits[i] * weights[i]
        print(f"  {digits[i]} * {weights[i]} = {digits[i] * weights[i]}")
    
    calculated = calculated % 11 % 10
    checksum = digits[9]
    
    print(f"Calculated checksum: {calculated}")
    print(f"Actual checksum: {checksum}")
    print(f"Valid: {calculated == checksum}")

if __name__ == "__main__":
    debug_inn_7707087890()