"""
MedForce Unified Server — Backward-compatibility shim.
The real application lives in medforce/app.py.
"""

from medforce.app import app  # noqa: F401

if __name__ == "__main__":
    import run  # noqa: F401
