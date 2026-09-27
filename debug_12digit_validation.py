"""Debug INN validation for 12-digit numbers."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_12digit_validation():
    print("=== Debug 12-digit INN Validation ===")
    
    detector = PIIDetector()
    
    # Test validation step by step
    test_inns = [
        "772855394477",  # 12 digits
        "770259728596",  # 12 digits
    ]
    
    for inn in test_inns:
        print(f"\nINN: {inn}")
        print(f"Length: {len(inn)}")
        
        # Check if it's 12 digits
        if len(inn) != 12:
            print("ERROR: Not 12 digits")
            continue
            
        # Extract digits for calculation
        digits = [int(d) for d in inn]
        print(f"Digits: {digits}")
        
        # Check if all digits are numeric
        if not inn.isdigit():
            print("ERROR: Contains non-digits")
            continue
            
        # Test validation method
        is_valid = detector._validate_inn(inn)
        print(f"Validation result: {is_valid}")
        
        # Manual calculation for 12-digit INN
        # d10 = (d1*1 + d2*2 + d3*3 + d4*4 + d5*5 + d6*6 + d7*7 + d8*8 + d9*9 + d10*10 + d11*11) mod 11
        # d11 = (d1*3 + d2*7 + d3*2 + d4*4 + d5*10 + d6*3 + d7*7 + d8*2 + d9*4 + d10*10 + d11*3) mod 11
        
        d1, d2, d3, d4, d5, d6, d7, d8, d9, d10, d11, d12 = digits
        
        # Calculate d10 (10th digit, index 9)
        sum_d10 = (d1*1 + d2*2 + d3*3 + d4*4 + d5*5 + d6*6 + d7*7 + d8*8 + d9*9 + d10*10 + d11*11)
        calculated_d10 = sum_d10 % 11
        if calculated_d10 > 9:
            calculated_d10 = calculated_d10 % 10
        print(f"Calculated d10: {calculated_d10}, actual d10: {d10}")
        
        # Calculate d11 (11th digit, index 10)  
        sum_d11 = (d1*3 + d2*7 + d3*2 + d4*4 + d5*10 + d6*3 + d7*7 + d8*2 + d9*4 + d10*10 + d11*3)
        calculated_d11 = sum_d11 % 11
        if calculated_d11 > 9:
            calculated_d11 = calculated_d11 % 10
        print(f"Calculated d11: {calculated_d11}, actual d11: {d11}")
        
        # Check if checksums match
        checksum_ok = (calculated_d10 == d10) and (calculated_d11 == d11)
        print(f"Checksum OK: {checksum_ok}")

if __name__ == "__main__":
    debug_12digit_validation()