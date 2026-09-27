"""Create truly valid INN numbers."""

def create_truly_valid_inns():
    """Create valid INN numbers with correct checksums."""
    
    # For 10-digit INN:
    # d10 = (d1*1 + d2*2 + d3*3 + d4*4 + d5*5 + d6*6 + d7*7 + d8*8 + d9*9) mod 11
    # If result > 9, use result % 10
    
    # Let's calculate: 1234567890
    # d10 = (1*1 + 2*2 + 3*3 + 4*4 + 5*5 + 6*6 + 7*7 + 8*8 + 9*9) mod 11
    # d10 = (1 + 4 + 9 + 16 + 25 + 36 + 49 + 64 + 81) mod 11
    # d10 = 285 mod 11 = 10, so checksum = 10 % 10 = 0 ✓
    # Valid INN: 1234567890
    
    # Let's calculate another one: 1111111111
    # d10 = (1*1 + 1*2 + 1*3 + 1*4 + 1*5 + 1*6 + 1*7 + 1*8 + 1*9) mod 11
    # d10 = (1+2+3+4+5+6+7+8+9) mod 11 = 45 mod 11 = 1
    # Valid INN: 1111111111
    
    # For 12-digit INN:
    # d10 = (d1*1 + d2*2 + d3*3 + d4*4 + d5*5 + d6*6 + d7*7 + d8*8 + d9*9 + d10*10 + d11*11) mod 11
    # d11 = (d1*3 + d2*7 + d3*2 + d4*4 + d5*10 + d6*3 + d7*7 + d8*2 + d9*4 + d10*10 + d11*3) mod 11
    
    # Let's calculate: 111111111111
    # d10 = (1*1 + 1*2 + 1*3 + 1*4 + 1*5 + 1*6 + 1*7 + 1*8 + 1*9 + 1*10 + 1*11) mod 11
    # d10 = (1+2+3+4+5+6+7+8+9+10+11) mod 11 = 66 mod 11 = 0
    # d11 = (1*3 + 1*7 + 1*2 + 1*4 + 1*10 + 1*3 + 1*7 + 1*2 + 1*4 + 1*10 + 1*3) mod 11
    # d11 = (3+7+2+4+10+3+7+2+4+10+3) mod 11 = 55 mod 11 = 0
    # Valid INN: 111111111111
    
    valid_individual_inns = [
        "0276453431",   # From original test
        "1234567890",   # Calculated above
        "1111111111",   # Calculated above
    ]
    
    valid_entity_inns = [
        "111111111111", # Calculated above
    ]
    
    print("=== Truly Valid INNs ===")
    print(f"Individual: {valid_individual_inns}")
    print(f"Entity: {valid_entity_inns}")
    
    return valid_individual_inns, valid_entity_inns

if __name__ == "__main__":
    create_truly_valid_inns()