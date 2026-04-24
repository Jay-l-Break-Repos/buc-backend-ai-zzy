#!/bin/bash

# Find the problematic import line
echo "Searching for the problematic import..."
grep -n "from warnings import deprecated" repo-vulnerable/src/ai/backend/common/types.py

# Replace it with Python 3.12 compatible version
echo "Fixing the import..."
sed -i 's/from warnings import deprecated/try:\n    from warnings import deprecated\nexcept ImportError:\n    from warnings import _deprecated as deprecated/' repo-vulnerable/src/ai/backend/common/types.py

echo "Done!"