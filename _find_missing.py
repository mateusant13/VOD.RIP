"""Find ALL names defined in session.py that warm.py uses but doesn't import."""
import re
from pathlib import Path

SESSION_PATH = 'C:/Users/Administrador/Desktop/Nova pasta (3)/TESTE/VOD.RIP/backend/services/preview/session.py'
WARM_PATH = 'C:/Users/Administrador/Desktop/Nova pasta (3)/TESTE/VOD.RIP/backend/services/preview/warm.py'
session = Path(SESSION_PATH).read_text()
warm = Path(WARM_PATH).read_text()

# Find ALL function/class/variable definitions in session.py (top-level)
session_defs = set()
for m in re.finditer(r'^(def |class |async def |    def )(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)', session, re.MULTILINE):
    session_defs.add(m.group('name'))
for m in re.finditer(r'^([A-Z_][A-Z_0-9]*)\s*=', session, re.MULTILINE):
    session_defs.add(m.group(1))

# Exclude Python builtins, module names, and stuff already fixed
session_defs -= {'logger'}

# Find what warm.py imports from session or _state (module-level or lazy)
warm_imports = set()
for m in re.finditer(r'from services\.preview\.(session|_state)\s+import\s+\(?([^)]*)\)?', warm, re.DOTALL):
    block = m.group(2)
    for token in re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', block):
        warm_imports.add(token)

# Find names that warm.py USES (identifier tokens) but doesn't define or import
warm_tokens = set(re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', warm))

# Names from session that warm uses but didn't import
missing = []
for name in sorted(session_defs):
    if name in warm_tokens and name not in warm_imports and not name.startswith('test'):
        missing.append(name)

print(f'{len(missing)} missing imports from session.py:')
for name in missing:
    # Find usage lines in warm.py
    lines = []
    for i, line in enumerate(warm.split('\n')):
        if name in line:
            stripped = line.strip()
            if not stripped.startswith('#') and not stripped.startswith('"""') and 'import' not in stripped:
                lines.append(f'    L{i+1}: {stripped[:100]}')
    print(f'\n  {name}')
    for l in lines[:3]:
        print(l)
