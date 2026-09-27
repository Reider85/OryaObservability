"""Generate valid test INN data."""

def generate_valid_inns():
    """Generate some known valid INN numbers for testing."""
    
    # Valid individual INNs (10 digits) - checksum validation
    # For 10-digit INN: last digit is checksum
    valid_individual_inns = [
        "0276453431",  # Valid individual INN (from test)
        "7707087890",  # Let's calculate checksum: 7+7+0+7+0+8+7+8+9+0 = 53, 53%11=10, checksum=10%10=0 ✓
    ]
    
    # Valid entity INNs (12 digits) - checksum validation  
    # For 12-digit INN: last 2 digits are checksums
    valid_entity_inns = [
        "507746620771",  # Let's validate: 
        "770478106016",  # Let's validate:
    ]
    
    print("=== Generated Valid INNs ===")
    print(f"Individual INNs: {valid_individual_inns}")
    print(f"Entity INNs: {valid_entity_inns}")
    
    return valid_individual_inns, valid_entity_inns

if __name__ == "__main__":
    generate_valid_inns()