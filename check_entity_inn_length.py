"""Check entity INN length."""

def check_entity_inn_length():
    inn = "1111111111136"
    print(f"Entity INN: {inn}")
    print(f"Length: {len(inn)}")
    print(f"Expected: 12")
    
    if len(inn) != 12:
        print("ERROR: Wrong length!")
        # Fix it
        inn = inn[:12]
        print(f"Fixed INN: {inn}")
        print(f"Fixed length: {len(inn)}")

if __name__ == "__main__":
    check_entity_inn_length()