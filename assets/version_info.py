# PyInstaller Windows version resource — taskbar / Properties show "VOD.RIP"
# ruff: noqa: F821  (VSVersionInfo/FixedFileInfo/etc. come from PyInstaller runtime, not real names)
VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=(1, 0, 18, 0),
        prodvers=(1, 0, 18, 0),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "mateusant13"),
                        StringStruct("FileDescription", "VOD.RIP — Kick & Twitch downloader"),
                        StringStruct("FileVersion", "1.0.21.0"),
                        StringStruct("InternalName", "VOD.RIP"),
                        StringStruct("LegalCopyright", "Copyright (c) mateusant13"),
                        StringStruct("OriginalFilename", "VOD-RIP.EXE"),
                        StringStruct("ProductName", "VOD.RIP"),
                        StringStruct("ProductVersion", "1.0.21.0"),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)
