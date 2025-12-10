import sys
import logging
from core import OdooAnalyzer


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m odecromancy.main <path_to_odoo_project>")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO)

    project_path = sys.argv[1]
    analyzer = OdooAnalyzer()

    print(f"Scanning {project_path}...")
    analyzer.scan_directory(project_path)

    print("Analyzing code...")
    analyzer.analyze()

    print("Generating Report...")
    analyzer.report()


if __name__ == "__main__":
    main()
