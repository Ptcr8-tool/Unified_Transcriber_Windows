"""Console-free fallback (requires Windows .pyw association with Python 3.11)."""
from pathlib import Path
import runpy
import sys
code_dir = Path(__file__).resolve().parent / '_internal'
sys.path.insert(0, str(code_dir))
runpy.run_path(str(code_dir / 'start_gui.pyw'), run_name='__main__')
