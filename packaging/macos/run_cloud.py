"""Keep source bootstrap settings identical to the frozen cloud edition."""
from pathlib import Path
import runpy
import sys

source = Path(__file__).resolve().parents[2] / "InterviewCopilot"
sys.path.insert(0, str(source))
sys._interview_cloud_edition = True
sys.argv[0] = str(source / "main.py")
runpy.run_path(sys.argv[0], run_name="__main__")
