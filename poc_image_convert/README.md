# 文書画像変換

PDF、PPTX、DOCX、XLSXをページ単位のPNGまたはWebP画像へ変換する。

## 対応形式

| 入力形式 | 出力単位 |
| --- | --- |
| PDF | 1ページにつき1画像 |
| PPTX | 1スライドにつき1画像 |
| DOCX | 1ページにつき1画像 |
| XLSX | 1印刷ページにつき1画像 |

## 準備

VS Codeでこのワークスペースを開き、`Dev Containers: Reopen in Container`を実行する。

## 使い方

`poc_image_convert`ディレクトリで実行する。

```bash
python convert_to_images.py <入力ファイル>
```

例:

```bash
python convert_to_images.py docs_input/samplefile.pdf
python convert_to_images.py docs_input/samplefile.pptx
python convert_to_images.py docs_input/samplefile.docx
python convert_to_images.py docs_input/samplefile.xlsx
```

既定では、入力ファイル名と同名のディレクトリを`docs_output`内に作成してPNGを出力する。

```text
docs_output/<入力ファイル名>/0001-page-1.png
docs_output/<入力ファイル名>/0002-page-2.png
```

## オプション

| オプション | 内容 | 既定値 |
| --- | --- | --- |
| `-o`, `--output-dir` | 出力先ディレクトリ | `docs_output/<入力ファイル名>/` |
| `--format` | `png`または`webp` | `png` |
| `--max-dimension` | 出力画像の長辺上限（px） | `2048` |
| `--pdf-dpi` | PDF描画時のDPI | `150` |

出力先と画像形式を指定する例:

```bash
python convert_to_images.py docs_input/samplefile.xlsx \
  --output-dir docs_output/xlsx-result \
  --format webp
```

すべてのオプションは次のコマンドで確認できる。

```bash
python convert_to_images.py --help
```