#!/usr/bin/env python3
"""
NIRSpec Data Reduction Pipeline CLI

Wrapper script to run the NIRSpec data reduction pipeline.

Usage:
    python scripts/reduce.py --obs ember_uds_p4 --extract
    python scripts/reduce.py --obs capers_cosmos_p1 --preprocess --extract
    python scripts/reduce.py --obs ember_uds_p4 --extract --processes 4
"""

import sys
from pathlib import Path

# Ensure we can import from the pipeline package
# Add project root to path if running from scripts/ directory
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import and run the main function from the pipeline
from pipeline.reduction import main

if __name__ == '__main__':
    main()
