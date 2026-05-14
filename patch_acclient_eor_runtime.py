"""
patch_acclient_eor_runtime.py — apply the Palette leak fix to a
running acclient.exe (EoR retail, version 0.0.11.6096) without
restarting it. The patch is the exact same six bytes as the on-disk
version; it just goes into the running process's code pages via
WriteProcessMemory instead of the file on disk.

Usage:
    # find all acclient.exe processes and patch them:
    python patch_acclient_eor_runtime.py

    # patch a specific PID:
    python patch_acclient_eor_runtime.py --pid 12345

    # revert (restore original bytes in the running process):
    python patch_acclient_eor_runtime.py --revert

    # dry-run — just print what would happen:
    python patch_acclient_eor_runtime.py --dry-run

Safety:
    - Verifies each target byte sequence matches what's expected before
      writing. If anything's off, that PID is skipped.
    - Idempotent: re-running on an already-patched process is a no-op.
    - On Windows only (uses kernel32 directly).
    - Requires Python to run with enough privilege to open the target
      process for VM_OPERATION + VM_WRITE. Usually fine if both Python
      and acclient.exe are launched by the same user.

What changes:
    Two 3-byte NOPs inside Palette::makeModifiedPalette overloads:
      VA 0x0053effe  ff 40 24  -> 90 90 90
      VA 0x0053f19c  ff 46 24  -> 90 90 90
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import sys


# The change in plain C:
#
#     Palette* Palette::makeModifiedPalette() {
#         void* p = operator new(0x48);
#         if (p == NULL) return NULL;
#         Palette::Palette(p, 0x800);     // ctor: p->refcount = 1
#   -     p->refcount += 1;                // <-- BUG: removed by this patch
#         return p;
#     }
#
# At the machine level, `p->refcount += 1` (with refcount at object
# offset +0x24) compiles to a 3-byte `inc dword [reg+0x24]`. We replace
# those 3 bytes with `90 90 90` (three NOPs) so the increment is
# skipped while the surrounding instructions are unchanged.
#
# (virtual_address, original_bytes, patched_bytes)
# Both sites are the same statement in two overloads of makeModifiedPalette:
#   VA 0x0053effe (no-arg overload):   inc dword [eax+0x24] -> NOP NOP NOP
#   VA 0x0053f19c (id, sub overload):  inc dword [esi+0x24] -> NOP NOP NOP
SITES = [
    (0x0053effe, bytes([0xff, 0x40, 0x24]), bytes([0x90, 0x90, 0x90])),
    (0x0053f19c, bytes([0xff, 0x46, 0x24]), bytes([0x90, 0x90, 0x90])),
]


PROCESS_VM_READ           = 0x0010
PROCESS_VM_WRITE          = 0x0020
PROCESS_VM_OPERATION      = 0x0008
PROCESS_QUERY_INFORMATION = 0x0400

PAGE_EXECUTE_READWRITE = 0x40

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = -1


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


k32 = ctypes.windll.kernel32
OpenProcess = k32.OpenProcess
OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
OpenProcess.restype = wt.HANDLE
CloseHandle = k32.CloseHandle
CloseHandle.argtypes = [wt.HANDLE]
CloseHandle.restype = wt.BOOL
ReadProcessMemory = k32.ReadProcessMemory
ReadProcessMemory.argtypes = [wt.HANDLE, wt.LPCVOID, wt.LPVOID,
                              ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
ReadProcessMemory.restype = wt.BOOL
WriteProcessMemory = k32.WriteProcessMemory
WriteProcessMemory.argtypes = [wt.HANDLE, wt.LPVOID, wt.LPCVOID,
                               ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
WriteProcessMemory.restype = wt.BOOL
VirtualProtectEx = k32.VirtualProtectEx
VirtualProtectEx.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t,
                             wt.DWORD, ctypes.POINTER(wt.DWORD)]
VirtualProtectEx.restype = wt.BOOL
CreateToolhelp32Snapshot = k32.CreateToolhelp32Snapshot
CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
CreateToolhelp32Snapshot.restype = wt.HANDLE
Process32FirstW = k32.Process32FirstW
Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
Process32FirstW.restype = wt.BOOL
Process32NextW = k32.Process32NextW
Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
Process32NextW.restype = wt.BOOL


def find_acclient_pids():
    pids = []
    snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return pids
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not Process32FirstW(snap, ctypes.byref(pe)):
            return pids
        while True:
            if pe.szExeFile.lower() == "acclient.exe":
                pids.append(pe.th32ProcessID)
            if not Process32NextW(snap, ctypes.byref(pe)):
                break
    finally:
        CloseHandle(snap)
    return pids


def patch_pid(pid, revert=False, dry_run=False):
    rights = (PROCESS_VM_READ | PROCESS_VM_WRITE
              | PROCESS_VM_OPERATION | PROCESS_QUERY_INFORMATION)
    h = OpenProcess(rights, False, pid)
    if not h:
        err = ctypes.get_last_error()
        print(f"PID {pid}: OpenProcess failed (err={err})")
        return False
    try:
        all_ok = True
        for va, orig, patched in SITES:
            cur = (ctypes.c_ubyte * len(orig))()
            sz = ctypes.c_size_t(0)
            if not ReadProcessMemory(h, va, cur, len(orig), ctypes.byref(sz)):
                print(f"PID {pid}: read 0x{va:08x} failed (err={ctypes.get_last_error()})")
                all_ok = False
                continue
            cur_b = bytes(cur)
            want = orig if revert else patched
            expect_before = patched if revert else orig

            if cur_b == want:
                print(f"PID {pid}: 0x{va:08x} already "
                      f"{'reverted' if revert else 'patched'}")
                continue
            if cur_b != expect_before:
                print(f"PID {pid}: 0x{va:08x} unexpected bytes "
                      f"{' '.join(f'{b:02x}' for b in cur_b)} "
                      f"(expected {' '.join(f'{b:02x}' for b in expect_before)}) — skipping")
                all_ok = False
                continue

            if dry_run:
                print(f"PID {pid}: 0x{va:08x} would "
                      f"{'revert' if revert else 'patch'}: "
                      f"{' '.join(f'{b:02x}' for b in cur_b)} -> "
                      f"{' '.join(f'{b:02x}' for b in want)}")
                continue

            old_prot = wt.DWORD(0)
            if not VirtualProtectEx(h, va, len(want), PAGE_EXECUTE_READWRITE,
                                    ctypes.byref(old_prot)):
                print(f"PID {pid}: VirtualProtectEx 0x{va:08x} failed "
                      f"(err={ctypes.get_last_error()})")
                all_ok = False
                continue
            buf = (ctypes.c_ubyte * len(want))(*want)
            if not WriteProcessMemory(h, va, buf, len(want), ctypes.byref(sz)):
                print(f"PID {pid}: write 0x{va:08x} failed "
                      f"(err={ctypes.get_last_error()})")
                all_ok = False
            restored = wt.DWORD(0)
            VirtualProtectEx(h, va, len(want), old_prot.value, ctypes.byref(restored))

            # Read back to confirm
            ReadProcessMemory(h, va, cur, len(want), ctypes.byref(sz))
            print(f"PID {pid}: 0x{va:08x} "
                  f"{' '.join(f'{b:02x}' for b in expect_before)} -> "
                  f"{' '.join(f'{b:02x}' for b in bytes(cur))} "
                  f"({'reverted' if revert else 'patched'})")
        return all_ok
    finally:
        CloseHandle(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pid", type=int, default=None,
                    help="patch a specific PID (default: all running acclient.exe)")
    ap.add_argument("--revert", action="store_true",
                    help="restore original bytes instead of patching")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    args = ap.parse_args()

    if args.pid is not None:
        pids = [args.pid]
    else:
        pids = find_acclient_pids()
        if not pids:
            print("no acclient.exe processes found")
            return 1
        print(f"found {len(pids)} acclient.exe process(es): {pids}")

    failures = 0
    for pid in pids:
        ok = patch_pid(pid, revert=args.revert, dry_run=args.dry_run)
        if not ok:
            failures += 1
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
