# This is a debug script to check the exact indentation
with open('C:\\projects\\OryaObservability\\tests\\test_pii_detector.py', 'r') as f:
    lines = f.readlines()
    
for i, line in enumerate(lines[475:485], 476):
    print(f"{i}: {repr(line)}")