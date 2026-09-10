"""Small, stable command-line boundary for the resumable TechPack workflow."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from techpack_pdf.errors import TechpackError
from techpack_pdf.workflow import analyze, apply, prepare_review


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise ValueError("invalid command arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="techpack-pdf", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    analyze_parser = commands.add_parser("analyze", add_help=False)
    analyze_parser.add_argument("source")
    analyze_parser.add_argument("--glossary", required=True)
    analyze_parser.add_argument("--job-dir", required=True)
    prepare_parser = commands.add_parser("prepare-review", add_help=False)
    prepare_parser.add_argument("--job", required=True)
    apply_parser = commands.add_parser("apply", add_help=False)
    apply_parser.add_argument("source")
    apply_parser.add_argument("--review", required=True)
    apply_parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "analyze":
            result = analyze(arguments.source, arguments.glossary, arguments.job_dir)
        elif arguments.command == "prepare-review":
            result = prepare_review(arguments.job)
        else:
            result = apply(arguments.source, arguments.review, arguments.output)
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        return result.exit_code
    except TechpackError as error:
        exit_code = 3 if error.code in {"mineru_unavailable", "mineru_invalid_response"} else 5 if error.code.startswith(("workflow_quality", "apply_", "review_")) else 2
        print(json.dumps({"code": error.code, "status": "failed"}, sort_keys=True), file=sys.stderr)
        return exit_code
    except (OSError, ValueError, argparse.ArgumentError, SystemExit):
        print(json.dumps({"code": "input_error", "status": "failed"}), file=sys.stderr)
        return 2
    except Exception:
        print(json.dumps({"code": "internal_error", "status": "failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
