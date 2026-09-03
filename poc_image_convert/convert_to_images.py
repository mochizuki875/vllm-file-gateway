#!/usr/bin/env python3
"""Backward-compatible entry point for document image conversion."""

from converter_cli import main
from converter_core import ConversionError, ConversionOptions, convert

__all__ = ["ConversionError", "ConversionOptions", "convert", "main"]


if __name__ == "__main__":
    raise SystemExit(main())