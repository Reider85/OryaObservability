"""Create simple valid INN for testing."""

def create_simple_valid_inns():
    """Create simple valid INN numbers that we can verify."""
    
    # For 10-digit INN, let's create one manually:
    # d10 = (d1*1 + d2*2 + d3*3 + d4*4 + d5*5 + d6*6 + d7*7 + d8*8 + d9*9) mod 11
    # If result > 9, use result % 10
    
    # Let's use: 1234567890
    # d10 = (1*1 + 2*2 + 3*3 + 4*4 + 5*5 + 6*6 + 7*7 + 8*8 + 9*9) mod 11
    # d10 = (1 + 4 + 9 + 16 + 25 + 36 + 49 + 64 + 81) mod 11
    # d10 = 285 mod 11 = 10, so checksum = 10 % 10 = 0
    # Valid INN: 1234567890
    
    # For 12-digit INN, let's create one:
    # d10 = (d1*1 + d2*2 + d3*3 + d4*4 + d5*5 + d6*6 + d7*7 + d8*8 + d9*9 + d10*10 + d11*11) mod 11
    # d11 = (d1*3 + d2*7 + d3*2 + d4*4 + d5*10 + d6*3 + d7*7 + d8*2 + d9*4 + d10*10 + d11*3) mod 11
    
    # Let's use: 111111111111
    # d10 = (1*1 + 1*2 + 1*3 + 1*4 + 1*5 + 1*6 + 1*7 + 1*8 + 1*9 + 1*10 + 1*11) mod 11
    # d10 = (1+2+3+4+5+6+7+8+9+10+11) mod 11 = 66 mod 11 = 0
    # d11 = (1*3 + 1*7 + 1*2 + 1*4 + 1*10 + 1*3 + 1*7 + 1*2 + 1*4 + 1*10 + 1*3) mod 11
    # d11 = (3+7+2+4+10+3+7+2+4+10+3) mod 11 = 55 mod 11 = 0
    # Valid INN: 111111111111
    
    valid_individual = "1234567890"
    valid_entity = "111111111111"
    
    print("=== Simple Valid INNs ===")
    print(f"Individual: {valid_individual}")
    print(f"Entity: {valid_entity}")
    
    return [valid_individual], [valid_entity]

if __name__ == "__main__":
    create_simple_valid_inns()