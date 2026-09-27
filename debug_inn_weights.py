"""Debug INN validation weights."""

def debug_inn_weights():
    print("=== Debug INN Validation Weights ===")
    
    # Check the weights used in the implementation
    weights_10 = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    
    print("10-digit INN weights:", weights_10)
    
    # Test with 1234567890
    inn = "1234567890"
    digits = [int(d) for d in inn]
    
    print(f"\nTesting INN: {inn}")
    print(f"Digits: {digits}")
    
    # Manual calculation with implementation weights
    calculated = 0
    for i in range(9):
        calculated += digits[i] * weights_10[i]
        print(f"  {digits[i]} * {weights_10[i]} = {digits[i] * weights_10[i]}")
    
    calculated = calculated % 11 % 10
    checksum = digits[9]
    
    print(f"\nCalculated checksum: {calculated}")
    print(f"Actual checksum: {checksum}")
    print(f"Valid: {calculated == checksum}")
    
    # Test with 1111111111
    inn2 = "1111111111"
    digits2 = [int(d) for d in inn2]
    
    print(f"\nTesting INN: {inn2}")
    print(f"Digits: {digits2}")
    
    calculated2 = 0
    for i in range(9):
        calculated2 += digits2[i] * weights_10[i]
        print(f"  {digits2[i]} * {weights_10[i]} = {digits2[i] * weights_10[i]}")
    
    calculated2 = calculated2 % 11 % 10
    checksum2 = digits2[9]
    
    print(f"\nCalculated checksum: {calculated2}")
    print(f"Actual checksum: {checksum2}")
    print(f"Valid: {calculated2 == checksum2}")

if __name__ == "__main__":
    debug_inn_weights()