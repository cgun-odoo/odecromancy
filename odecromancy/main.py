import sys
import logging
import argparse
from .core import OdooAnalyzer


def main():
    parser = argparse.ArgumentParser(prog="Odecromancy", usage='%(prog)s project_path [options]')
    parser.add_argument("project_path")
    parser.add_argument("-i", "--ignore")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    project_path = args.project_path
    ignore_file_path = args.ignore
    analyzer = OdooAnalyzer()

    print(f"Scanning {project_path}...")
    analyzer.scan_directory(project_path, ignore_file_path=ignore_file_path)

    print("Analyzing code...")
    analyzer.analyze()

    print("Generating Report...")
    analyzer.report()


if __name__ == "__main__":
    main()
