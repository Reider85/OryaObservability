# This is a debug script to check more lines
with open('C:\\projects\\OryaObservability\\tests\\test_pii_detector.py', 'r') as f:
    lines = f.readlines()
    
for i, line in enumerate(lines[470:490], 471):
    print(f"{i}: {repr(line)}")