"""Create valid INN using implementation weights."""

def create_valid_inn_with_weights():
    """Create valid INN using the weights from the implementation."""
    
    weights_10 = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    
    # Let's create a valid 10-digit INN
    # We need to find digits d1-d9 such that:
    # checksum = (d1*2 + d2*4 + d3*10 + d4*3 + d5*5 + d6*9 + d7*4 + d8*6 + d9*8) % 11 % 10
    
    # Let's try with all 1s: 111111111?
    digits = [1, 1, 1, 1, 1, 1, 1, 1, 1]
    calculated = 0
    for i in range(9):
        calculated += digits[i] * weights_10[i]
    
    calculated = calculated % 11 % 10
    checksum = calculated
    
    valid_inn = "111111111" + str(checksum)
    print(f"Created valid INN: {valid_inn}")
    
    # Verify
    digits2 = [int(d) for d in valid_inn]
    calculated2 = 0
    for i in range(9):
        calculated2 += digits2[i] * weights_10[i]
    
    calculated2 = calculated2 % 11 % 10
    checksum2 = digits2[9]
    
    print(f"Verification: calculated={calculated2}, actual={checksum2}, valid={calculated2 == checksum2}")
    
    return valid_inn

def create_valid_entity_inn():
    """Create valid 12-digit entity INN."""
    
    # For 12-digit INN, we need to use the weights from the implementation
    weights1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    weights2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    
    # Let's try with all 1s: 11111111111?
    digits = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    
    # Calculate first checksum (10th digit)
    calculated1 = 0
    for i in range(10):
        calculated1 += digits[i] * weights1[i]
    calculated1 = calculated1 % 11 % 10
    checksum1 = calculated1
    
    # Calculate second checksum (11th digit)
    calculated2 = 0
    for i in range(11):
        calculated2 += digits[i] * weights2[i]
    calculated2 = calculated2 % 11 % 10
    checksum2 = calculated2
    
    valid_inn = "11111111111" + str(checksum1) + str(checksum2)
    print(f"Created valid entity INN: {valid_inn}")
    
    return valid_inn

if __name__ == "__main__":
    valid_individual = create_valid_inn_with_weights()
    valid_entity = create_valid_entity_inn()
    
    print(f"\nValid individual INN: {valid_individual}")
    print(f"Valid entity INN: {valid_entity}")