"""Debug INN regex pattern step by step."""

import re

def debug_inn_step_by_step():
    test_cases = [
        "0276453431",    # 10 digits
        "7707087890",    # 10 digits
        "5077466207712", # 12 digits
        "7704781060168", # 12 digits
    ]
    
    pattern1 = r'\b[0-9]{10}\b'
    pattern2 = r'\b[0-9]{12}\b'
    combined = r'\b[0-9]{10}\b|\b[0-9]{12}\b'
    
    print("=== Testing individual patterns ===")
    for inn in test_cases:
        m1 = re.findall(pattern1, inn)
        m2 = re.findall(pattern2, inn)
        m_combined = re.findall(combined, inn)
        
        print(f"INN: {inn}")
        print(f"  10-digit pattern: {m1}")
        print(f"  12-digit pattern: {m2}")
        print(f"  Combined: {m_combined}")
    
    print("\n=== Testing in text ===")
    for inn in test_cases:
        text = f"INN: {inn}"
        m_combined = re.findall(combined, text)
        print(f"Text: '{text}' -> {m_combined}")

if __name__ == "__main__":
    debug_inn_step_by_step()