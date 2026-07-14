# VOD.RIP-remake — Agent Instructions

## consultgpt — Terminal ChatGPT (No API Key)

consultgpt is installed globally and available as `gpt` and `codeintel` commands.

It uses a **real browser session** to interact with ChatGPT. No API key needed — just log in once.

### Quick Reference

| Command | What it does |
|---------|--------------|
| `gpt "question"` | One-shot question to ChatGPT |
| `gpt -f file.py "review"` | Inject file contents + ask |
| `gpt @src/main.py "explain"` | Same via @file syntax |
| `gpt -s name "question"` | Start named session (persistent) |
| `gpt -s name "follow-up"` | Continue previous session |
| `gpt kill name` | Kill a session |
| `gpt audit` | Full codebase audit |
| `codeintel search "query"` | Search code index |
| `codeintel ask "how does X work?"` | Synthesize architecture answer |

### Code Review Loop (MANDATORY)

Every significant code change must go through this loop:

```
1. codeintel search "concept"     → find relevant files
2. make changes                   → implement
3. gpt -f changed.py "review"     → get ChatGPT review
4. fix issues found               → iterate
5. done                           → only when gpt says pass
```

### File Injection

```bash
# Inject single file
gpt -f backend/services/preview_service.py "Review for bugs"

# Inject multiple files
gpt -f file1.py -f file2.py "Review these two files"

# Line ranges
gpt -f backend/app.py:50-100 "Explain this section"

# @file syntax (inline in question)
gpt "Review @backend/services/preview_service.py for security issues"
```

### Multi-Turn Sessions

```bash
# Start a named session (injects code on first turn)
gpt -s review "Review @src/main.py for bugs"

# Follow-up (context preserved automatically)
gpt -s review "Now fix the issues you found"

# Verify fixes
gpt -s review "Verify my changes are correct"

# Kill when done
gpt kill review
```

### Code Index (codeintel)

```bash
# Index the project
codeintel index .

# Search for symbols
codeintel search "PreviewSession"

# Search for callers
codeintel search "callers of create_session"

# Ask architectural questions
codeintel ask "how does the preview pipeline work?"

# Code health check
codeintel health
```

### Audit Mode

```bash
# Full codebase audit
gpt audit

# Audit specific files
gpt audit --files backend/app.py

# Audit specific folders
gpt audit --folders backend/services/
```

### Flags

| Flag | Purpose |
|------|---------|
| `-f, --files` | Inject files as code context |
| `-s, --session` | Named session for persistence |
| `--no-code` | Skip code injection (required when no files) |
| `--headed` | Show browser window |
| `--auto` | Auto-route based on prompt size |
| `--kill-after N` | Timeout in minutes |
| `--codeintel` | Use index for search (NOT for local file review) |

### Common Patterns for This Project

```bash
# Review a router change
gpt -f backend/routers/preview.py "Review for API correctness"

# Review a service change
gpt -f backend/services/preview_service.py "Check for race conditions"

# Review frontend changes
gpt -f src/App.tsx "Review for React best practices"

# Full backend audit
gpt audit --folders backend/

# Find where a function is used
codeintel search "callers of schedule_youtube_window_hls_mux"
```

### Rules

1. **Always review after changes** — `gpt -f changed_file.py "review"` before commit
2. **Use codeintel first** — search before writing code
3. **Don't mix --codeintel and -f** — they're different paths
4. **--kill-after on long runs** — prevent runaway processes
5. **CLI only** — use terminal commands, not workarounds

### Windows/PowerShell Notes

- `--files a b c` works with space-separated paths
- Use `--` to separate flags from question: `gpt -f a.py -- "review this"`
- Progress prints to stderr, response to stdout
