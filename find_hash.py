#!/usr/bin/env python3

import hashlib

# Find trace IDs that will produce hash values around 49 and 50
def find_trace_id_for_hash(target_hash):
    for i in range(1000):
        trace_id = f"test_trace_{i:03d}"
        trace_hash = int(hashlib.sha256(trace_id.encode("utf-8")).hexdigest(), 16) % 100
        if trace_hash == target_hash:
            return trace_id, trace_hash
    return None, None

# Find IDs for hash 49 and 50
trace_id_49, hash_49 = find_trace_id_for_hash(49)
trace_id_50, hash_50 = find_trace_id_for_hash(50)

print(f"For hash 49: trace_id = {trace_id_49}, hash = {hash_49}")
print(f"For hash 50: trace_id = {trace_id_50}, hash = {hash_50}")

# Also check what the current test IDs produce
current_ids = ["test_trace_49", "test_trace_50"]
for trace_id in current_ids:
    trace_hash = int(hashlib.sha256(trace_id.encode("utf-8")).hexdigest(), 16) % 100
    print(f"Current {trace_id}: hash = {trace_hash}")