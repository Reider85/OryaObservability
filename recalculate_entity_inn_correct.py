"""Recalculate entity INN with correct approach."""

def recalculate_entity_inn_correct():
    """Recalculate entity INN with correct approach."""
    
    weights1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    weights2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    
    # Start with 10 digits: 1111111111
    digits = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    
    # Calculate first checksum (10th digit, index 9)
    calculated1 = 0
    for i in range(10):
        calculated1 += digits[i] * weights1[i]
    calculated1 = calculated1 % 11 % 10
    checksum1 = calculated1
    
    # Add first checksum
    digits.append(checksum1)
    
    # Calculate second checksum (11th digit, index 10)
    calculated2 = 0
    for i in range(11):
        calculated2 += digits[i] * weights2[i]
    calculated2 = calculated2 % 11 % 10
    checksum2 = calculated2
    
    # Add second checksum
    digits.append(checksum2)
    
    valid_inn = ''.join(str(d) for d in digits)
    print(f"Corrected entity INN: {valid_inn}")
    print(f"Length: {len(valid_inn)}")
    
    # Verify
    digits2 = [int(d) for d in valid_inn]
    
    # First checksum
    checksum1_verify = int(digits2[10])
    calculated1_verify = 0
    for i in range(10):
        calculated1_verify += digits2[i] * weights1[i]
    calculated1_verify = calculated1_verify % 11 % 10
    
    # Second checksum
    checksum2_verify = int(digits2[11])
    calculated2_verify = 0
    for i in range(11):
        calculated2_verify += digits2[i] * weights2[i]
    calculated2_verify = calculated2_verify % 11 % 10
    
    print(f"First checksum: calculated={calculated1_verify}, actual={checksum1_verify}, valid={calculated1_verify == checksum1_verify}")
    print(f"Second checksum: calculated={calculated2_verify}, actual={checksum2_verify}, valid={calculated2_verify == checksum2_verify}")
    print(f"Overall valid: {calculated1_verify == checksum1_verify and calculated2_verify == checksum2_verify}")
    
    return valid_inn

if __name__ == "__main__":
    recalculate_entity_inn_correct()