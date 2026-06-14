# Code signing for VOD.RIP

> Closes ANTIVIRUS_AUDIT findings F4 and F13. Until this is in place, every
> first-time installer download triggers a "Windows protected your PC"
> SmartScreen prompt, and VirusTotal reports 5-15 detections of the
> `PUA:Win32/UnsignedInstaller` and `RiskWare.Tool.Installer` families.

## Why

SmartScreen blocks any unsigned binary that has not accumulated reputation
through volume. Acquiring an EV (Extended Validation) certificate bypasses the
reputation check entirely; an OV (Organization Validation) certificate still
requires ~3,000 successful installs before SmartScreen stops prompting.

EV certs from DigiCert, Sectigo, or GlobalSign are US$240-500/year and ship
with a hardware token (USB dongle) for the signing key.

## Local signing

1. Drop your `.pfx` at `signing/vodrip.pfx` (the path is referenced from
   `installer/installer.iss` via the `#ifexist` preprocessor directive).
2. Set environment variables before running the build:
   ```bash
   export VODRIP_CERT_FILE="$PWD/signing/vodrip.pfx"
   export VODRIP_CERT_PWD="..."        # use a secret manager; not a plain env in CI
   export VODRIP_TIMESTAMP="http://timestamp.digicert.com"
   ```
3. Build the EXE (`npm run build-dist`). The PyInstaller bootloader is
   produced at `release/vod-rip/VOD-RIP.exe`; the Inno Setup installer is
   produced at `release/VOD.RIP-<ver>-Setup.exe`.
4. Sign the bootloader (PyInstaller's bootloader is signable):
   ```bash
   signtool sign /fd SHA256 /tr "$VODRIP_TIMESTAMP" /td sha256 \
                 /f "$VODRIP_CERT_FILE" /p "$VODRIP_CERT_PWD" \
                 "release/vod-rip/VOD-RIP.exe"
   ```
5. Sign the installer (signtool picks it up via Inno Setup's [Setup]
   `SignTool=vodrip` directive, or you can sign it manually):
   ```bash
   signtool sign /fd SHA256 /tr "$VODRIP_TIMESTAMP" /td sha256 \
                 /f "$VODRIP_CERT_FILE" /p "$VODRIP_CERT_PWD" \
                 "release/VOD.RIP-<ver>-Setup.exe"
   ```
6. Verify:
   ```bash
   signtool verify /pa "release/VOD.RIP-<ver>-Setup.exe"
   ```

## CI signing

The CI workflow is in `.github/workflows/release.yml`. Add a job that:

1. Installs the Windows SDK (signtool.exe) on the runner.
2. Decodes the `.pfx` from a GitHub Actions secret:
   ```yaml
   - name: Decode signing certificate
     shell: pwsh
     run: |
       $pfxBytes = [Convert]::FromBase64String($env:VODRIP_CERT_B64)
       [IO.File]::WriteAllBytes("signing/vodrip.pfx", $pfxBytes)
   env:
     VODRIP_CERT_B64: ${{ secrets.VODRIP_CERT_B64 }}
   ```
3. Runs `npm run build-dist` (the EXE and the installer are produced).
4. Signs both artefacts (see steps 4-5 above).
5. Replaces the un-signed artefacts in the release with the signed ones.

Store the certificate as a base64-encoded secret
(`VODRIP_CERT_B64 = base64 -i vodrip.pfx`) and the password as
`VODRIP_CERT_PWD`. Never commit a `.pfx` to the repository.

## SmartScreen reputation loop

EV certs skip the reputation check, so this is a one-time investment. For
projects that stay on OV certs, reputation is built by users clicking "Run
anyway" and Windows learning that the file is safe. This takes months and
hurts conversion — EV is recommended.

## What this fixes

- SmartScreen "Windows protected your PC" prompt on first run
- `PUA:Win32/UnsignedInstaller` and `RiskWare.Tool.Installer` on VirusTotal
- `Behavior:Win32/LargeUnsignedBinaryLaunch` from Defender
- YARA rules that key on `PyInstaller/Trojanized` (some engines whitelist
  signed PyInstaller bootloaders)

## What this does NOT fix

- The dynamic, runtime download + PowerShell + robocopy flow in
  `services/updater.py` — see ANTIVIRUS_AUDIT F1. Even with a signed
  binary, the in-app self-update is a top-tier EDR heuristic and should
  be replaced with an external download page or a small signed
  `VOD-RIP-Updater.exe` shipped beside the app.
- The bundled ffmpeg.exe / ffprobe.exe — see ANTIVIRUS_AUDIT F2. Bundling
  third-party unsigned binaries keeps the `PUA:Win32/BundledTool` flag
  active even when the launcher is signed.
