"""Container entrypoint: load file-based secrets, then run the OpsFleet CLI.

For each VAR in SECRET_VARS, if VAR_FILE names a file (a Compose secret under
/run/secrets), its contents become VAR. This must happen before main.py is imported
because src/gemini.py reads GEMINI_API_KEY at import time.
"""

import os
import runpy
import sys
from pathlib import Path

SECRET_VARS = ('GEMINI_API_KEY', 'DATACHAT_DB_PASSWORD')

for var in SECRET_VARS:
    path = os.environ.pop(var + '_FILE', None)
    if path:
        os.environ[var] = Path(path).read_text(encoding='utf-8').strip()

sys.argv = ['main.py', *sys.argv[1:]]
runpy.run_path('/app/main.py', run_name='__main__')
