#!/usr/bin/env python3

import os
import re

# Path to the types.py file
types_file = "repo-vulnerable/src/ai/backend/common/types.py"

# Read the file
with open(types_file, 'r') as f:
    content = f.read()

# Find and replace the problematic import
# Replace "from warnings import deprecated" with a Python 3.12 compatible version
old_import = "from warnings import deprecated"
new_import = """try:
    from warnings import deprecated
except ImportError:
    # Python 3.12 compatibility
    from warnings import _deprecated as deprecated"""

if old_import in content:
    print(f"Found problematic import: {old_import}")
    content = content.replace(old_import, new_import)
    
    # Write back the fixed content
    with open(types_file, 'w') as f:
        f.write(content)
    print("Fixed the import!")
else:
    print("Import not found, searching for other patterns...")
    # Search for any line containing 'deprecated'
    lines = content.split('\n')
    for i, line in enumerate(lines):
        if 'deprecated' in line and 'import' in line:
            print(f"Line {i+1}: {line}")