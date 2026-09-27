"""Debug recall rate more thoroughly."""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agent_obs.guardrail.pii_detector import PIIDetector

def debug_recall_rate_thorough():
    print("=== Debug Recall Rate Thoroughly ===")
    
    detector = PIIDetector()
    
    # Test dataset
    test_dataset = [
        # Email examples (20)
        ("Contact me at alice@example.com", ["alice@example.com"]),
        ("Email: bob@test.org", ["bob@test.org"]),
        ("Support: help@company.co.uk", ["help@company.co.uk"]),
        ("Send to user+filter@domain.io", ["user+filter@domain.io"]),
        ("UPPERCASE: TEST@DOMAIN.COM", ["TEST@DOMAIN.COM"]),
        ("Mixed: TeSt@DoMaIn.Net", ["TeSt@DoMaIn.Net"]),
        ("With dash: user-name@domain.org", ["user-name@domain.org"]),
        ("With dot: first.last@company.com", ["first.last@company.com"]),
        ("Numbers: user123@test123.com", ["user123@test123.com"]),
        ("Complex: a.b+c_d@sub.domain.co.uk", ["a.b+c_d@sub.domain.co.uk"]),
        ("Short: a@b.cd", ["a@b.cd"]),
        ("Long: very.long.email.address@very.long.domain.name.com", ["very.long.email.address@very.long.domain.name.com"]),
        ("Multiple: user1@domain.com, user2@test.org", ["user1@domain.com", "user2@test.org"]),
        ("In sentence: Please email contact@support.com for help.", ["contact@support.com"]),
        ("In quotes: 'my.email@example.com' is my email.", ["my.email@example.com"]),
        ("In parentheses: (email: test@domain.org)", ["test@domain.org"]),
        ("With brackets: [user@example.com]", ["user@example.com"]),
        ("With braces: {user@domain.com}", ["user@domain.com"]),
        ("With angle brackets: <contact@company.com>", ["contact@company.com"]),
        ("Context: For more info, email info@company.com or call support.", ["info@company.com", "support"]),
    
        # Phone examples (20)
        ("Call +7 (999) 123-45-67", ["+7 (999) 123-45-67"]),
        ("Phone: 89001234567", ["89001234567"]),
        ("Mobile: +79991234567", ["+79991234567"]),
        ("International: +1 555-123-4567", ["+1 555-123-4567"]),
        ("UK: +44 20 7946 0958", ["+44 20 7946 0958"]),
        ("No spaces: +79991234567", ["+79991234567"]),
        ("With dots: +7.999.123.45.67", ["+7.999.123.45.67"]),
        ("With dashes: +7-999-123-45-67", ["+7-999-123-45-67"]),
        ("In text: My phone is +7 (999) 123-45-67", ["+7 (999) 123-45-67"]),
        ("Multiple: +7 (999) 123-45-67 and 89001234567", ["+7 (999) 123-45-67", "89001234567"]),
        ("Russian: 89001234567, +7 (999) 123-45-67", ["89001234567", "+7 (999) 123-45-67"]),
        ("International: +1 (555) 123-4567", ["+1 (555) 123-4567"]),
        ("US: +1 555-123-4567", ["+1 555-123-4567"]),
        ("Canada: +1 (416) 555-1234", ["+1 (416) 555-1234"]),
        ("Europe: +49 30 1234567", ["+49 30 1234567"]),
        ("Asia: +81 3-1234-5678", ["+81 3-1234-5678"]),
        ("Australia: +61 2 1234 5678", ["+61 2 1234 5678"]),
        ("In parentheses: Phone: (+7 999) 123-45-67", ["(+7 999) 123-45-67"]),
        ("In quotes: '+7 (999) 123-45-67' is my number", ["+7 (999) 123-45-67"]),
        ("Context: Call +7 (999) 123-45-67 or +1 555-123-4567", ["+7 (999) 123-45-67", "+1 555-123-4567"]),
    
        # INN examples (20)
        ("INN: 0276453431", ["0276453431"]),  # Valid individual
        ("Tax ID: 7707087890", ["7707087890"]),  # Valid individual
        ("Entity INN: 111111111130", ["111111111130"]),  # Valid entity
        ("Company: 111111111130", ["111111111130"]),  # Valid entity
        ("Invalid INN: 1234567890", []),  # Invalid checksum
        ("Short INN: 123456789", []),  # Too short
        ("Long INN: 123456789012345", []),  # Too long
        ("In text: My INN is 0276453431", ["0276453431"]),
        ("Multiple: INN 0276453431 and 7707087890", ["0276453431", "7707087890"]),
        ("Entity: Company INN 5077466207712", ["5077466207712"]),  # Still old invalid
        ("In parentheses: (INN: 0276453431)", ["0276453431"]),
        ("In quotes: '0276453431' is my INN", ["0276453431"]),
        ("Context: INN 0276453431 for tax purposes", ["0276453431"]),
        ("With spaces: INN 027 645 343 1", []),  # Spaces detected as invalid
        ("Mixed: INN0276453431", ["INN0276453431"]),  # Without spaces
        ("In sentence: The INN number is 7707087890.", ["7707087890"]),
        ("In list: INNs: 0276453431, 7707087890", ["0276453431", "7707087890"]),
        ("In document: Entity INN: 5077466207712", ["5077466207712"]),  # Still old invalid
        ("In form: Enter INN: 7704781060168", ["7704781060168"]),  # Still old invalid
        ("In table: | INN | 0276453431 |", ["0276453431"]),
    
        # Passport examples (20)
        ("Passport: 1234 567890", ["1234 567890"]),
        ("ID: 9876 543210", ["9876 543210"]),
        ("Document: 1234 567890", ["1234 567890"]),  # With space
        ("Invalid: 123 45678", []),  # Wrong length
        ("With letters: ABC123456", []),  # Contains letters
        ("In text: My passport is 1234 567890", ["1234 567890"]),
        ("Multiple: Passport 1234 567890 and ID 9876 543210", ["1234 567890", "9876 543210"]),
        ("In parentheses: (Passport: 1234 567890)", ["1234 567890"]),
        ("In quotes: '1234 567890' is my passport", ["1234 567890"]),
        ("Context: Passport number 1234 567890", ["1234 567890"]),
        ("In form: Passport: 1234 567890", ["1234 567890"]),
        ("In document: Document ID: 9876 543210", ["9876 543210"]),
        ("In table: | Passport | 1234 567890 |", ["1234 567890"]),
        ("In sentence: The passport number is 1234 567890.", ["1234 567890"]),
        ("In list: Passports: 1234 567890, 9876 543210", ["1234 567890", "9876 543210"]),
        ("In email: passport:1234 567890", ["1234 567890"]),
        ("In address: Address with passport 1234 567890", ["1234 567890"]),
        ("In contract: Passport: 1234 567890", ["1234 567890"]),
        ("In application: ID number: 9876 543210", ["9876 543210"]),
        ("In system: Passport 1234 567890 registered", ["1234 567890"]),
        ("In database: Record: 1234 567890", ["1234 567890"]),
    
        # Payment card examples (20)
        ("Card: 4111111111111111", ["4111111111111111"]),  # Valid Visa test
        ("Payment: 5555555555554444", ["5555555555554444"]),  # Valid Mastercard test
        ("Credit: 378282246310005", ["378282246310005"]),  # Valid Amex
        ("Debit: 3530111333300000", ["3530111333300000"]),  # Valid JCB
        ("Invalid: 4111111111111112", []),  # Invalid checksum
        ("With letters: ABC123456789012", []),  # Contains letters
        ("In text: My card is 4111111111111111", ["4111111111111111"]),
        ("Multiple: Card 4111111111111111 and 5555555555554444", ["4111111111111111", "5555555555554444"]),
        ("In parentheses: (Card: 4111111111111111)", ["4111111111111111"]),
        ("In quotes: '4111111111111111' is my card", ["4111111111111111"]),
        ("Context: Card number 4111111111111111", ["4111111111111111"]),
        ("In form: Card: 4111111111111111", ["4111111111111111"]),
        ("In document: Payment: 5555555555554444", ["5555555555554444"]),
        ("In table: | Card | 4111111111111111 |", ["4111111111111111"]),
        ("In sentence: The card number is 4111111111111111.", ["4111111111111111"]),
        ("In list: Cards: 4111111111111111, 5555555555554444", ["4111111111111111", "5555555555554444"]),
        ("In email: card:4111111111111111", ["4111111111111111"]),
        ("In transaction: Payment 5555555555554444", ["5555555555554444"]),
        ("In checkout: Card: 4111111111111111", ["4111111111111111"]),
        ("In app: Card number 4111111111111111", ["4111111111111111"]),
        ("In database: Card: 4111111111111111", ["4111111111111111"]),
    ]
    
    total_entities = 0
    detected_entities = 0
    failed_cases = []
    
    for i, (text, expected_entities) in enumerate(test_dataset):
        matches = detector.detect(text)
        actual_entities = [match.value for match in matches]
        
        total_entities += len(expected_entities)
        detected_in_case = 0
        
        for expected in expected_entities:
            if expected in actual_entities:
                detected_in_case += 1
        
        if detected_in_case < len(expected_entities):
            failed_cases.append((i, text, expected_entities, actual_entities, detected_in_case))
    
    print(f"Total entities: {total_entities}")
    print(f"Failed cases: {len(failed_cases)}")
    
    for i, text, expected, actual, detected in failed_cases[:10]:  # Show first 10 failures
        print(f"\nCase {i}: '{text}'")
        print(f"  Expected: {expected}")
        print(f"  Actual: {actual}")
        print(f"  Detected: {detected}/{len(expected)}")
    
    recall_rate = detected_entities / total_entities if total_entities > 0 else 0
    print(f"\nRecall rate: {recall_rate:.3f}")

if __name__ == "__main__":
    debug_recall_rate_thorough()