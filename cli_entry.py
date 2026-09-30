#!/usr/bin/env python3
"""Console entry point for the LUFS Normalizer CLI, bundled as LUFSNormalizer_v3.1.4_CLI.exe."""
import multiprocessing
multiprocessing.freeze_support()
from lufs_normalizer.cli import main

if __name__ == '__main__':
    main()
