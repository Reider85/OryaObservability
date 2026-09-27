"""Create valid INN to replace 7707087890."""

def create_valid_replacement_inn():
    """Create a valid INN to replace the invalid one."""
    
    weights = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    
    # Let's use: 111111111
    digits = [1, 1, 1, 1, 1, 1, 1, 1, 1]
    
    calculated = 0
    for i in range(9):
        calculated += digits[i] * weights[i]
    
    calculated = calculated % 11 % 10
    checksum = calculated
    
    valid_inn = ''.join(str(d) for d in digits) + str(checksum)
    print(f"Valid replacement INN: {valid_inn}")
    
    # Verify
    digits2 = [int(d) for d in valid_inn]
    calculated2 = 0
    for i in range(9):
        calculated2 += digits2[i] * weights[i]
    
    calculated2 = calculated2 % 11 % 10
    checksum2 = digits2[9]
    
    print(f"Verification: calculated={calculated2}, actual={checksum2}, valid={calculated2 == checksum2}")
    
    return valid_inn

if __name__ == "__main__":
    create_valid_replacement_inn()