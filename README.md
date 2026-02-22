# Ham Radio Exam Result Extractor

Parse ham radio exam result PDFs from examtools.org into structured JSON and CSV, including aggregate analytics for:

- question-level performance (`T3A04`)
- section-level performance (`T3`)
- subsection-level performance (`T3A`)

This tool supports:

- single-exam PDFs
- concatenated multi-exam PDFs (multiple exams in one file)
- multiple input files in one run

**NOTE**: This tool has only been tested on macOS as of 2026-02-21

## Requirements

- Python 3.10+ (tested with Python 3)
- [Ghostscript](https://www.ghostscript.com/) available in `PATH` as `gs` (required for PDF input)

Ghostscript install examples:

- macOS (Homebrew): `brew install ghostscript`
- Ubuntu/Debian: `sudo apt-get install ghostscript`
- Windows (winget): `winget install ArtifexSoftware.Ghostscript`

## Installation

No package install is required.

```bash
git clone https://github.com/jpasqua/ham_extract_results
cd extract_results
python3 -m py_compile extract_results.py
```

## Quick Start

Single PDF to JSON:

```bash
python3 extract_results.py /path/to/results.pdf --out results.json --pretty
```

Single PDF to question-row CSV:

```bash
python3 extract_results.py /path/to/results.pdf --csv questions.csv
```

Multiple files into one combined output:

```bash
python3 extract_results.py /path/a.pdf /path/b.pdf /path/c.pdf \
  --out all_results.json \
  --csv all_questions.csv \
  --stats-csv question_stats.csv \
  --section-stats-csv section_stats.csv \
  --subsection-stats-csv subsection_stats.csv
```

## CLI Usage

```text
python3 extract_results.py INPUT [INPUT ...] [options]
```

Positional inputs:

- `INPUT`: one or more files (`.pdf` or plain text)

Options:

- `--input-type {auto,pdf,text}`: default `auto` (by file extension)
- `--out PATH`: write JSON output to file (otherwise prints to stdout)
- `--csv PATH`: write flattened question rows CSV
- `--stats-csv PATH`: write per-question stats CSV
- `--section-stats-csv PATH`: write per-section stats CSV
- `--subsection-stats-csv PATH`: write per-subsection stats CSV
- `--pretty`: pretty-print JSON

## Input Format Notes

Question rows are parsed from text matching patterns like:

```text
17. T5B04: D
17. T5B04: D (should be A)
```

Where:

- `17` is question number in the exam
- `T5B04` is question identifier
- `D` is selected answer
- `(should be A)` indicates an incorrect response and the correct answer

## Output Behavior

### One input file

If the file contains one exam:

- output includes `metadata`, `summary`, and `questions`

If the file contains concatenated exams:

- output includes top-level `metadata`, `summary`, and `exams` list

### Multiple input files

Output is aggregated into one JSON object with:

- `sources`
- `summary` (overall totals)
- `question_stats`
- `section_stats`
- `subsection_stats`
- `results` (full parsed output per source file)

Here is a snipet of the output JSON file showing a couple of the questions encountered:

```json
  "questions": [
    {
      "number": 1,
      "question_id": "T7A05",
      "selected": "D",
      "correct": "D",
      "is_correct": true
    },
    {
      "number": 2,
      "question_id": "T4A07",
      "selected": "C",
      "correct": "C",
      "is_correct": true
    },
```

## CSV Outputs

### `--csv`

Flattened question rows with columns:

- `source`
- `exam_index_in_source`
- `test_number`
- `element`
- `number`
- `question_id`
- `selected`
- `correct`
- `is_correct`

### `--stats-csv`

Per-question aggregate columns:

- `question_id`
- `attempts`
- `correct`
- `incorrect`
- `accuracy`

### `--section-stats-csv`

Per-section aggregate columns:

- `section_id` (e.g. `T3`, `G2`, `E1`)
- `attempts`
- `correct`
- `incorrect`
- `accuracy`

### `--subsection-stats-csv`

Per-subsection aggregate columns:

- `subsection_id` (e.g. `T3A`, `G2E`, `E1C`)
- `attempts`
- `correct`
- `incorrect`
- `accuracy`

All stats CSV outputs are sorted hardest-first (lowest `accuracy` first).

## Error Handling

Common failure cases:

- input file path does not exist
- Ghostscript (`gs`) is not installed or not in `PATH`
- Ghostscript fails to read an invalid/corrupt PDF

The script prints a clear `ERROR:` line to stderr and exits non-zero.

## License

MIT. See [`LICENSE`](LICENSE).
