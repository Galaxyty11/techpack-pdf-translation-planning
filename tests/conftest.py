from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "translating-techpack-pdfs" / "scripts"
sys.path.insert(0, str(SCRIPTS))
