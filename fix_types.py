#!/usr/bin/env python3

# Read the types.py file
with open('repo-vulnerable/src/ai/backend/common/types.py', 'r') as f:
    content = f.read()

# Replace the problematic import
old_import = "from warnings import deprecated"
new_import = """try:
    from warnings import deprecated
except ImportError:
    # Python 3.12 compatibility - deprecated was added in 3.13
    from warnings import _deprecated as deprecated"""

print(f"Looking for: {old_import}")
if old_import in content:
    print("Found the problematic import, fixing it...")
    content = content.replace(old_import, new_import)
    
    # Write the fixed content back
    with open('repo-vulnerable/src/ai/backend/common/types.py', 'w') as f:
        f.write(content)
    print("Fixed the import successfully!")
else:
    print("Import not found in the file")
    # Let's search for any line containing deprecated
    lines = content.split('\n')
    for i, line in enumerate(lines, 1):
        if 'deprecated' in line.lower():
            print(f"Line {i}: {line}")