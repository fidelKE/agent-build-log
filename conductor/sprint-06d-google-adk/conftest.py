import sys
from pathlib import Path

# Ensure the local src/ is resolved first when the shared venv has multiple
# sprint packages installed under the same 'src' name.
sys.path.insert(0, str(Path(__file__).parent))
