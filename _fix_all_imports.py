"""Add ALL missing lazy imports from session.py to the right functions in warm.py."""
import re
from pathlib import Path

WARM = Path('C:/Users/Administrador/Desktop/Nova pasta (3)/TESTE/VOD.RIP/backend/services/preview/warm.py')
SESSION = Path('C:/Users/Administrador/Desktop/Nova pasta (3)/TESTE/VOD.RIP/backend/services/preview/session.py')

warm_lines = WARM.read_text().split('\n')
warm_text = WARM.read_text()

# Map: function_name -> set of missing names to import at its start
missing = {
    '_try_adopt_preflight_mux': {
        '_merge_youtube_session_cookies', '_pick_variant_by_height',
        'WINDOW_HLS_INITIAL_CHUNK_SEC',
    },
    '_build_youtube_session_snapshot': {
        '_hosts_for_url', '_apply_muxed_progressive_session',
        '_apply_youtube_custom_master', '_build_synthetic_master_playlist',
        '_clamp_session_crop_to_vod_duration',
    },
    'warm_youtube_resolve_only': {
        '_pick_variant_by_height', '_hosts_for_url',
    },
    '_maybe_try_adopt_preflight_mux': {
        '_pick_variant_by_height',
    },
    '_full_mux_cache_path': {
        '_full_mux_cache_path',  # Might be defined in session, used as `_full_mux_cache_path(vid, ...)` wait no
    },
}

# Also find all references in the wholetest and add imports for anything not covered:
# Map: usage line -> function -> missing name
usage_map = {}
for i, line in enumerate(warm_lines):
    for name in ['_hosts_for_url', '_apply_muxed_progressive_session', 
                 '_apply_youtube_custom_master', '_build_synthetic_master_playlist',
                 '_clamp_session_crop_to_vod_duration', '_merge_youtube_session_cookies',
                 '_pick_variant_by_height', 'WINDOW_HLS_INITIAL_CHUNK_SEC',
                 'MuxJob', '_full_mux_cache_path', '_prog_head_paths',
                 'create_session']:
        if name in line and 'import' not in line and not line.strip().startswith('#'):
            # Find containing function
            func = None
            for j in range(i, -1, -1):
                s = warm_lines[j].strip()
                if s.startswith('def ') or s.startswith('async def '):
                    func = s.split('(')[0].replace('def ','').replace('async def ','')
                    break
            if func:
                usage_map.setdefault(func, set()).add(name)

# Now for each function with missing imports, add them
added = 0
for func_name, names in sorted(usage_map.items()):
    if not names:
        continue
    
    for i, line in enumerate(warm_lines):
        if line.strip().startswith(f'def {func_name}(') or line.strip().startswith(f'async def {func_name}('):
            # Find body start (skip params, docstring)
            j = i + 1
            parens = line.count('(') - line.count(')')
            while j < len(warm_lines) and parens > 0:
                s = warm_lines[j].strip()
                parens += s.count('(') - s.count(')')
                j += 1
            # Skip blank lines + docstring
            while j < len(warm_lines):
                s = warm_lines[j].strip()
                if s == '':
                    j += 1
                elif s.startswith('"""'):
                    j += 1
                    while j < len(warm_lines) and not warm_lines[j].strip().endswith('"""'):
                        j += 1
                    if warm_lines[j].strip().endswith('"""'):
                        j += 1
                else:
                    break
            
            # Check which names are already imported (scan backward from j within the function)
            existing_imports = set()
            for k in range(i, j):
                if 'from services.preview.session import' in warm_lines[k]:
                    for token in re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', warm_lines[k].split('import')[1]):
                        existing_imports.add(token)
            
            still_needed = names - existing_imports
            if not still_needed:
                continue
            
            # Also check ALL lazy imports in the function (after body start too)
            for k in range(j, min(j+50, len(warm_lines))):
                if 'from services.preview.session import' in warm_lines[k]:
                    for token in re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', warm_lines[k].split('import')[1]):
                        still_needed.discard(token)
            
            if not still_needed:
                continue
            
            # Add import line
            indent = warm_lines[j][:len(warm_lines[j]) - len(warm_lines[j].lstrip())]
            names_str = ', '.join(sorted(still_needed))
            warm_lines.insert(j, f'{indent}from services.preview.session import {names_str}')
            added += 1
            print(f'{func_name}: added {names_str} at line {j+1}')
            break

WARM.write_text('\n'.join(warm_lines))
print(f'Total: {added} import groups added')
