# This is a debug script to check even more lines
with open('C:\\projects\\OryaObservability\\tests\\test_pii_detector.py', 'r') as f:
    lines = f.readlines()
    
for i, line in enumerate(lines[465:495], 466):
    print(f"{i}: {repr(line)}")