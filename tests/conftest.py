import os
import sys

sys.dont_write_bytecode = True  # nu lasa __pycache__ in src/backend
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "backend"))
