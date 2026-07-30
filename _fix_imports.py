#!/usr/bin/env python3
"""Add missing lazy imports from session.py to warm.py functions."""
import re
import sys
from pathlib import Path

warm_path = Path('backend/services/preview/warm.py')
lines = warm_path.read_text().split('\n')

# Map: function_name -> set of missing names
# Only the functions that need them (skip if already have import nearby)
need = {
    '_youtube_preflight_mux': {'_youtube_needs_dash_window_hls'},
    '_build_youtube_session_snapshot': {
        '_resolve_preview_entry', '_stash_youtube_preview_formats',
        '_init_window_hls_mux_bounds', '_youtube_muxed_progressive_for_long_explore',
    },
    '_try_adopt_preflight_mux': {
        '_window_hls_dir', '_window_hls_seg0_ready',
    },
    'warm_youtube_preview_resolve': {
        'kickoff_youtube_prog_head_warm',
    },
}

# Find each function and add import after docstring
for func_name, missing in need.items():
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f'def {func_name}(') or stripped.startswith(f'async def {func_name}('):
            # Find body start (skip params, docstring)
            j = i + 1
            # Skip multi-line params
            paren_depth = stripped.count('(') - stripped.count(')')
            while j < len(lines) and paren_depth > 0:
                s = lines[j].strip()
                paren_depth += s.count('(') - s.count(')')
                j += 1
            # Skip blank lines + docstring
            while j < len(lines):
                s = lines[j].strip()
                if s == '' or s.startswith('"""'):
                    j += 1
                    # Skip the rest of the docstring
                    while j < len(lines) and not lines[j].strip().endswith('"""'):
                        j += 1
                    if lines[j].strip().endswith('"""'):
                        j += 1
                else:
                    break
            # Check which names are already imported nearby (within 5 lines before j)
            existing = set()
            for k in range(max(0, j-5), j):
                if 'from services.preview.session import' in lines[k]:
                    content = lines[k].split('import')[1]
                    for token in re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', content):
                        existing.add(token)
            # Which names still need import?
            still_needed = missing - existing
            if not still_needed:
                print(f'{func_name}: all already imported')
                continue
            
            # Add import line
            indent = lines[j][:len(lines[j]) - len(lines[j].lstrip())]
            names_str = ', '.join(sorted(still_needed))
            lines.insert(j, f'{indent}from services.preview.session import {names_str}')
            print(f'{func_name}: added {names_str}')
            break
    else:
        print(f'{func_name}: FUNCTION NOT FOUND')

warm_path.write_text('\n'.join(lines))
print('Done')
