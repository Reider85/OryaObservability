"""Test +44 pattern step by step."""

import re

def step_by_step_test():
    text = "+44 20 7946 0958"
    
    # Test components
    print("=== Testing components ===")
    
    # Test +44 part
    pattern1 = r'\+44'
    matches1 = re.findall(pattern1, text)
    print(f"Pattern '{pattern1}': {matches1}")
    
    # Test digits after +44
    pattern2 = r'\+44\s[0-9]{4}'
    matches2 = re.findall(pattern2, text)
    print(f"Pattern '{pattern2}': {matches2}")
    
    # Test full pattern with literal spaces
    pattern3 = r'\+44\s20\s7946\s0958'
    matches3 = re.findall(pattern3, text)
    print(f"Pattern '{pattern3}': {matches3}")
    
    # Test the pattern with actual spaces
    pattern4 = r'\+44\s\d{4}\s\d{4}\s\d{2}'
    matches4 = re.findall(pattern4, text)
    print(f"Pattern '{pattern4}': {matches4}")
    
    # Test character class
    pattern5 = r'\+44\s[0-9 ]+'
    matches5 = re.findall(pattern5, text)
    print(f"Pattern '{pattern5}': {matches5}")

if __name__ == "__main__":
    step_by_step_test()