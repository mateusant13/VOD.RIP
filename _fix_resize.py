import re

with open('src/App.tsx', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Fix the timeline div: use maxHeight instead of height
old1 = '''style={trimPanelHeight > 0 ? { height: trimPanelHeight + 'px', overflowY: 'auto' } : undefined}'''
new1 = '''style={trimPanelHeight > 0 ? { maxHeight: trimPanelHeight + 'px', overflowY: 'auto' } : undefined}'''
assert old1 in content, "timeline div style not found"
content = content.replace(old1, new1, 1)

# 2. Fix the resize handle: use delta-based sizing
old_handle = '''            <div
            className="h-2 cursor-ns-resize flex items-center justify-center gap-1 select-none shrink-0 hover:bg-zinc-800/50 rounded"
            onPointerDown={(e) => {
              e.preventDefault();
              e.currentTarget.setPointerCapture(e.pointerId);
              trimPanelResizeRef.current = true;
            }}
            onPointerMove={(e) => {
              if (!trimPanelResizeRef.current) return;
              const panel = e.currentTarget.parentElement;
              if (!panel) return;
              const rect = panel.getBoundingClientRect();
              const h = Math.max(100, e.clientY - rect.top);
              setTrimPanelHeight(h);
            }}
            onPointerUp={(e) => {
              trimPanelResizeRef.current = false;
              try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
            }}
            onPointerCancel={(e) => {
              trimPanelResizeRef.current = false;
              try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
            }}
          >'''

assert old_handle in content, "resize handle not found"

new_handle = '''            <div
            className="h-2 cursor-ns-resize flex items-center justify-center gap-1 select-none shrink-0 hover:bg-zinc-800/50 rounded"
            onPointerDown={(e) => {
              e.preventDefault();
              e.currentTarget.setPointerCapture(e.pointerId);
              trimPanelResizeRef.current = true;
              const panel = e.currentTarget.parentElement;
              if (panel) {
                const rect = panel.getBoundingClientRect();
                (e.currentTarget as any).__startY = e.clientY;
                (e.currentTarget as any).__startH = trimPanelHeight > 0 ? trimPanelHeight : rect.height;
              }
            }}
            onPointerMove={(e) => {
              if (!trimPanelResizeRef.current) return;
              const startY = (e.currentTarget as any).__startY;
              const startH = (e.currentTarget as any).__startH;
              if (startY == null || startH == null) return;
              const delta = e.clientY - startY;
              const h = Math.max(100, startH + delta);
              setTrimPanelHeight(h);
            }}
            onPointerUp={(e) => {
              trimPanelResizeRef.current = false;
              delete (e.currentTarget as any).__startY;
              delete (e.currentTarget as any).__startH;
              try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
            }}
            onPointerCancel={(e) => {
              trimPanelResizeRef.current = false;
              delete (e.currentTarget as any).__startY;
              delete (e.currentTarget as any).__startH;
              try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
            }}
          >'''

content = content.replace(old_handle, new_handle, 1)

with open('src/App.tsx', 'w', encoding='utf-8') as f:
    f.write(content)

print("OK: fixes applied")
