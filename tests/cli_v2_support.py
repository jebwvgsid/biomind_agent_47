"""V2 entry to the committed guarded offline runner; no new ambient config."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli_model_progress_support import launch, guarded_child, main, PYTHON, DEPS

if __name__ == '__main__':
    main()
