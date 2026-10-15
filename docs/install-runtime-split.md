# Split-Runtime Install — light base, opt-in ASR runtime

Status: **implemented and measured 2026-10-15** (branch `main`, app version 1.0.53).
This file replaces guessing with the measurements behind the split; the code
references are exact so the numbers can be re-derived.

## Why

The owner directive: the app has real users now, so releases must land working,
and the install must stay light — nothing heavy until the user agrees.

## What the base bundle is (measured)

| | files | size |
|---|---|---|
| before (committed `vod-rip.spec`) | 19 214 | **1 210.9 MB** |
| after (this change) | 6 833 | **199.0 MB** |

-83.6% / 6.1x smaller. Build: `pyinstaller vod-rip.spec --noconfirm` (same
command `npm run build-dist` uses), measured on the produced `dist/VOD-RIP`
tree, work/dist paths redirected to H: so C: was not touched.

### Where the 1 GB actually came from (not a guess)

The committed spec already excluded `torch`, `sherpa_onnx`, `ctranslate2`,
`onnxruntime`, `panns_inference`, `silero_vad`, `tokenizers` — and those
exclusions worked (their PyInstaller hooks never ran). The leak was the
**transitive** closure, traced from `build/VOD-RIP/xref-VOD-RIP.html` of the
pre-fix build (modulegraph follows imports inside functions, so lazy imports
still enter the graph):

```text
services.archive_transcribe
  -> huggingface_hub
     -> huggingface_hub._webhooks_server   (experimental HF Spaces feature)
        -> gradio -> gradio.pipelines -> transformers
           -> torchvision / torchao / timm / datasets / accelerate / faiss
           -> soynlp / babel / spacy / thinc / blis / srsly / cv2 / av
           -> pandas / pyarrow / skimage (gradio + fsspec.arrow + tqdm.std)
  torch's own DLLs additionally rode in through PyInstaller's binary
  dependency analysis of torchvision/_C.pyd (libtorch_cpu.dll alone is 241 MB).

services.archive_transcribe -> services.archive_events -> librosa
  -> sklearn / numba / llvmlite / soynlp
```

Every one of those is a *function-level* import in `backend/` (e.g.
`archive_transcribe.py:216 import torch`, inside `_torch_cuda_vram`), or is
never imported by the base at all — which is why excluding them changes no
behaviour: the app degrades exactly as it already did for `torch` & friends.

### What changed

`vod-rip.spec` — the base spec's `excludes` list now also names the
transitive majors (`huggingface_hub._webhooks_server`, `gradio`,
`transformers`, `datasets`, `accelerate`, `torchvision`, `torchao`, `timm`,
`faiss`, `librosa`, `soynlp`, `pandas`, `pyarrow`, `sklearn`, `numba`,
`llvmlite`, `spacy`, `cv2`, `av`, `babel`). One list addition; nothing else
in the build moved.

The heavy stack itself is unchanged and still shipped — as the **optional**
runtime: `vod-rip-asr.spec` + `backend/asr_worker.py` -> `VOD-RIP-ASR.exe`.

## The opt-in install flow

```text
Inno Setup (installer/installer.iss)
  [Types]  light (DEFAULT) | full
  [Components] asr -> Types: full   (so it starts UNCHECKED by default)
  [Files]  ..\scripts\install-asr-runtime.ps1  Components: asr
  [Run]    powershell ... install-asr-runtime.ps1  Components: asr
```

- Light install: only `staging\*` (the base bundle). Nothing heavy.
- Full install (or ticking the ASR box on the components page): the script
  runs during setup and installs the runtime.

`scripts/install-asr-runtime.ps1` mirrors the app's own contract
(`backend/services/asr_runtime.py`), so an installer-installed runtime is
byte-for-byte what the app expects:

1. manifest fetched over **HTTPS** (plain HTTP refused; test-switch
   `-AllowLocalFile` exists for offline verification only and is not used by
   the installer); same URL default as the app, honouring
   `VODRIP_ASR_RUNTIME_MANIFEST_URL` for private mirrors.
2. manifest schema validated: `version`, `executable`, `archive_url`,
   `archive_sha256`, per-file `files` checksum map.
3. archive sha256 verified **before** extraction.
4. every extracted file is hashed while being written and compared;
   unlisted members, absolute paths, drive prefixes and `..` are refused.
5. writes the `.runtime.json` marker (`{"version","executable"}`) that
   `runtime_available()` looks for; destination swap is all-or-nothing.
6. installs to the user's AI-models folder when settings.json has
   `whisper_model_cache`, otherwise `%LOCALAPPDATA%\VOD.RIP\asr-runtime`;
   only in that fallback case is `VODRIP_ASR_RUNTIME_DIR` pinned at User
   scope (the env var the app reads). No elevation, no `pip`.
7. Inno's uninstall removes the pinned env var and the payload when the
   component was installed (the shipped script is the marker for that).

The in-app path is unchanged and still available: `POST /api/asr/runtime`
(`routers/system.py::install_asr_runtime`) downloads, verifies and extracts
the same archive through `services/asr_runtime.py`. A failed opt-in install
never blocks the base install.

## Building the pieces

```powershell
# base bundle (light) - work/dist/TMP on H:, then install to H:
powershell -ExecutionPolicy Bypass -File scripts\build-install.ps1

# optional ASR runtime: pyinstaller + stage + zip + sha256 manifest
powershell -ExecutionPolicy Bypass -File scripts\build-asr-runtime.ps1 -Version 1.0.53
```

`scripts/build-asr-runtime.ps1` chains the existing primitives:
`pyinstaller vod-rip-asr.spec --clean --noconfirm`,
`node scripts/deploy-asr-runtime.mjs` (stages
`dist/runtimes/asr/<version>/`), `Compress-Archive` to
`release/VOD-RIP-ASR-<version>-win-x64.zip`, then
`node scripts/write-asr-runtime-manifest.mjs <version>` (per-file sha256 +
archive sha256 — the manifest the installer script verifies against). It
needs the `+cuda` wheel installed (`backend/requirements-gpu.txt`, as the CI
"Install GPU ASR runtime deps" step does); without it the runtime still
builds but degrades to the onnxruntime CPU EP.

## Proof performed (2026-10-15, all on H:)

- **size**: 1 210.9 MB / 19 214 files -> 199.0 MB / 6 833 files (same
  `pyinstaller vod-rip.spec`, before/after the excludes).
- **hook evidence**: heavy-stack hooks processed by PyInstaller 14 -> 0.
- **boot without the runtime**: `VOD-RIP.exe` (lean bundle) booted in 2.7 s,
  `/api/info` -> `desktop=true`, version 1.0.53; `/api/settings` -> OK;
  `/api/asr/runtime` -> `installed=false` (no crash — the negative test is
  the default state).
- **red arm for the smoke harness**: a copy of the bundle with
  `_internal/python311.dll` overwritten did NOT boot (harness detects a
  broken bundle; the pristine copy beside it did).
- **red arms for the install script** (fake runtime + real manifests):
  tampered file -> checksum mismatch refused; unlisted archive member
  refused; zip-slip member refused; plain-HTTP manifest refused; happy path
  installs + writes the marker; re-run is a no-op.
- **installer end-to-end** (compiled with ISCC, dummy staging):
  `/TYPE=light /SILENT` installs the app WITHOUT
  `scripts/install-asr-runtime.ps1`; `/TYPE=full /SILENT` installs it and
  runs it (transcript on disk shows the download attempt). Both uninstall
  clean; no env vars or payload left behind.

## The optional ASR runtime bundle (measured, fix applied, post-fix size NOT measured)

Building the runtime as the committed `vod-rip-asr.spec` stood produced
**8 850 MB / 24 851 files** (measured 2026-10-15 by running
`pyinstaller vod-rip-asr.spec`; the artifact was deleted again to free H:).
The same transitive leak inflated it: `asr_hidden_imports()` pulls
`services.archive_transcribe`, whose `huggingface_hub` ->
`_webhooks_server` -> `gradio` -> `transformers` chain drags in the entire
training/inference stack the runtime never uses.

The same excludes were therefore added to `vod-rip-asr.spec`, keeping
everything the runtime actually imports (`torch`, `torchaudio`,
`sherpa_onnx`, `ctranslate2`, `onnxruntime`, `panns_inference`,
`silero_vad`, `librosa` + `numba`/`llvmlite`/`sklearn`, `soundfile`).
Partial evidence that the chain is cut: in the post-fix build log the
`hook-torch.py` hook runs (correct - the runtime needs torch) while
`hook-gradio`, `hook-transformers`, `hook-torchvision` and `hook-spacy` run
**0** times. The post-fix **size was not measured** - the rebuild did not
finish inside this session's budget (the machine was under load and the
torch hook's `collect_submodules` crawls). Re-measure with
`scripts/build-asr-runtime.ps1` before quoting a number.

## Not proven here / follow-ups

- The `VOD-RIP-ASR.exe` runtime **was** built (8 850 MB, see the section above)
  and then deleted to free H:. The post-exclude rebuild is the one thing left
  unmeasured; `scripts/build-asr-runtime.ps1` is the entry point and the CI
  workflow already stages every piece.
- No real runtime archive was downloaded end-to-end (the release asset does
  not exist yet for this version). The download path is the same code the app
  already uses (`asr_runtime.ensure_runtime`, covered by
  `backend/tests/test_asr_runtime_status.py`), and the installer script was
  proven against local manifests/archives including tamper arms.
- Version pinning between app build and runtime archive is a release-process
  guarantee (same tag builds both); neither the app nor the installer script
  hard-fails on a version mismatch — both log it. Make it a fail-closed
  check if the owner wants that.
