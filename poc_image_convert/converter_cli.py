"""Command-line interface for document image conversion."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from converter_core import ConversionError, ConversionOptions, convert


DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parent / "docs_output"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("0より大きい整数を指定してください")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PDF/PPTX/DOCX/XLSXを画像へ変換します。"
    )
    parser.add_argument("input_file", type=Path, help="変換するファイル")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="出力先。省略時はdocs_output/<入力ファイル名>/",
    )
    parser.add_argument(
        "--format",
        choices=("png", "webp"),
        default="png",
        dest="image_format",
        help="出力形式 (default: png)",
    )
    parser.add_argument(
        "--max-dimension",
        type=positive_int,
        default=2048,
        help="画像の長辺上限px (default: 2048)",
    )
    parser.add_argument(
        "--pdf-dpi",
        type=positive_int,
        default=150,
        help="PDFの描画DPI (default: 150)",
    )
    return parser.parse_args(argv)


def build_options(args: argparse.Namespace, input_path: Path) -> ConversionOptions:
    output_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / input_path.stem
    return ConversionOptions(
        output_dir=output_dir.resolve(),
        image_format=args.image_format,
        max_dimension=args.max_dimension,
        pdf_dpi=args.pdf_dpi,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = args.input_file.expanduser().resolve()
    if not input_path.is_file():
        print(f"error: ファイルが見つかりません: {input_path}", file=sys.stderr)
        return 2

    options = build_options(args, input_path)
    try:
        outputs = convert(input_path, options)
    except ConversionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"converted: {input_path}")
    print(f"output: {options.output_dir}")
    print(f"images: {len(outputs)}")
    for output in outputs:
        print(f"  {output.name}")
    return 0