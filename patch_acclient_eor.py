"""
patch_acclient_eor.py — fix the Palette memory leak in the retail
Asheron's Call EoR client (acclient.exe, version 0.0.11.6096).

Usage:
    python patch_acclient_eor.py <path-to-acclient.exe>

What it does:
    1. Verifies the input file matches the known EoR SHA-256
    2. Writes acclient.eor.orig.exe next to it (unmodified backup)
    3. Writes acclient.eor.patched.exe (the fixed binary)
    4. Verifies the patched SHA-256 matches the expected value

What the patch changes:
    Two 3-byte NOPs at file offsets 0x13effe and 0x13f19c.
    Both sites are buggy `inc dword [reg+0x24]` instructions inside
    Palette::makeModifiedPalette overloads — the bug leaves each newly-
    created modified palette at refcount=2 instead of refcount=1, so it
    never gets destroyed and leaks 8 KB of ARGB data plus the object.

To deploy after running this script:
    1. Close all running acclient.exe processes
    2. Back up your original acclient.exe (or trust acclient.eor.orig.exe
       is correct — it's a byte-for-byte copy)
    3. Replace acclient.exe with acclient.eor.patched.exe
    4. Launch normally

To revert:
    Replace acclient.exe with acclient.eor.orig.exe.

Requirements: Python 3, no third-party packages.
"""
import hashlib
import os
import shutil
import sys


# EoR retail acclient.exe (version 0.0.11.6096, timestamp 0x557a956c)
EXPECTED_INPUT_SHA256 = "bca95bbebed4b9ed1ff09d0da83144e2fc4208f63ad7ada5cb47c3ca207ccba9"
EXPECTED_INPUT_SIZE   = 4841472

# After patching
EXPECTED_OUTPUT_SHA256 = "69ac75174a0ea0f5a1fcd1c17bad2a562fad6439e984f05ff103a44e02bf4fc1"
EXPECTED_OUTPUT_SIZE   = 4841472   # same — only in-place NOPs

# The change in plain C (mirroring the actual decompile):
#
#     Palette* Palette::makeModifiedPalette() {
#         Palette* p = (Palette*)operator new(0x48);
#         if (p == NULL) return NULL;
#         p = Palette::Palette(p, 0x800);   // ctor: p->refcount = 1
#         if (p != NULL) {
#   -         p->refcount += 1;              // <-- BUG: removed by this patch
#         }
#         return p;
#     }
#
# The `if (p != NULL)` after the ctor is a compiler-emitted defensive
# check; the ctor never actually returns NULL. The bug is the increment
# inside it.
#
# At the machine level, `p->refcount += 1` (with refcount at object
# offset +0x24) compiles to a 3-byte `inc dword [reg+0x24]`. We replace
# those 3 bytes with `90 90 90` (three NOPs) so the increment is
# skipped while the surrounding instructions and the function size are
# unchanged.
#
# (file_offset, original_bytes, patched_bytes)
# Both sites are the same statement in two overloads of makeModifiedPalette:
#   0x13effe (no-arg overload): inc dword [eax+0x24]  -> NOP NOP NOP
#   0x13f19c (id, sub overload): inc dword [esi+0x24]  -> NOP NOP NOP
PATCHES = [
    (0x0013effe, bytes([0xff, 0x40, 0x24]), bytes([0x90, 0x90, 0x90])),
    (0x0013f19c, bytes([0xff, 0x46, 0x24]), bytes([0x90, 0x90, 0x90])),
]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if len(sys.argv) < 2:
        print(f"usage: python {sys.argv[0]} <path-to-acclient.exe>")
        sys.exit(2)

    src_path = sys.argv[1]

    if not os.path.isfile(src_path):
        print(f"ERROR: not a file: {src_path}")
        sys.exit(2)

    # Validate input
    size = os.path.getsize(src_path)
    if size != EXPECTED_INPUT_SIZE:
        print(f"ERROR: input size {size} does not match expected {EXPECTED_INPUT_SIZE}")
        print("This script only works on the EoR retail acclient.exe "
              "(version 0.0.11.6096).")
        sys.exit(3)

    print(f"input:        {src_path}")
    print(f"input size:   {size}")
    print("hashing input file...", flush=True)
    src_sha = sha256_of(src_path)
    print(f"input sha256: {src_sha}")

    if src_sha != EXPECTED_INPUT_SHA256:
        print(f"ERROR: input sha256 does not match expected EoR value.")
        print(f"  expected: {EXPECTED_INPUT_SHA256}")
        print(f"  actual:   {src_sha}")
        print("This script only works on the EoR retail acclient.exe.")
        sys.exit(3)

    # Write outputs next to the input
    base_dir = os.path.dirname(os.path.abspath(src_path))
    orig_path    = os.path.join(base_dir, "acclient.eor.orig.exe")
    patched_path = os.path.join(base_dir, "acclient.eor.patched.exe")

    print()
    print(f"writing backup:  {orig_path}")
    shutil.copy2(src_path, orig_path)

    print(f"writing patched: {patched_path}")
    with open(src_path, "rb") as f:
        data = bytearray(f.read())

    for off, orig_bytes, patched_bytes in PATCHES:
        actual = bytes(data[off:off + len(orig_bytes)])
        if actual != orig_bytes:
            print(f"ERROR: bytes at offset 0x{off:08x} do not match expected.")
            print(f"  expected: {' '.join(f'{b:02x}' for b in orig_bytes)}")
            print(f"  actual:   {' '.join(f'{b:02x}' for b in actual)}")
            sys.exit(4)
        data[off:off + len(patched_bytes)] = patched_bytes
        print(f"  patched offset 0x{off:08x}: "
              f"{' '.join(f'{b:02x}' for b in orig_bytes)} -> "
              f"{' '.join(f'{b:02x}' for b in patched_bytes)}")

    with open(patched_path, "wb") as f:
        f.write(data)

    out_size = os.path.getsize(patched_path)
    out_sha  = sha256_of(patched_path)
    print()
    print(f"patched size:   {out_size}")
    print(f"patched sha256: {out_sha}")

    if out_sha != EXPECTED_OUTPUT_SHA256:
        print(f"ERROR: patched sha256 does not match expected.")
        print(f"  expected: {EXPECTED_OUTPUT_SHA256}")
        print(f"  actual:   {out_sha}")
        sys.exit(5)

    print()
    print("OK: patched binary verified.")
    print()
    print("Next steps:")
    print("  1. Close all running acclient.exe processes.")
    print("  2. Replace your installed acclient.exe with the file:")
    print(f"     {patched_path}")
    print("  3. Launch as usual.")
    print()
    print(f"To revert: copy {orig_path} back over acclient.exe.")


if __name__ == "__main__":
    main()
