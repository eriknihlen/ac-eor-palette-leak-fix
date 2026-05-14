# Asheron's Call EoR — Palette memory-leak fix

Six-byte binary patch to the retail Asheron's Call End-of-Retail client
(`acclient.exe`, version 0.0.11.6096) that eliminates the dominant
memory leak driving the multi-day-uptime crashes.

This is **not a different client** — it's the same `acclient.exe`
with six bytes replaced by NOPs. Same DLLs, same server, same UDP
protocol, no DLL injection, no third-party code.

## What's the leak?

The class `Palette` (used by AC's palette-shift system for equipment
color customization, character appearance, item icon tinting, and
landscape detail tinting) has two factory methods —
`Palette::makeModifiedPalette()` and
`Palette::makeModifiedPalette(DataID, Subpalette*)` — that both do an
extra `refcount++` after construction. Each newly-created modified
palette returns at `refcount = 2` instead of `refcount = 1`. The
existing cleanup chain only does one matching release, which brings
the refcount down to 1, **not 0**. The palette and its 8 KB ARGB
buffer never get destroyed.

In a 27-hour soak dump, this produced **56,664 leaked Palette
instances** holding ~446 MB of orphaned ARGB buffers. Over multiple
days of play, the leak walks the 32-bit client into its 2 GB virtual
ceiling and the process crashes.

## What the patch does (as code)

Both 2013 (full PDB available) and EoR have an identical
structural bug. The decompiled body of the no-arg overload:

```c
// Palette::makeModifiedPalette()
// EoR address 0x0053EFE0, 2013 address 0x0053E280

Palette* Palette::makeModifiedPalette() {
    Palette* p = (Palette*)operator new(0x48);   // 72-byte Palette
    if (p == NULL) return NULL;

    p = Palette::Palette(p, 0x800);              // ctor sets p->refcount = 1
                                                 // and returns this
    if (p != NULL) {                             // (ctor never actually returns
                                                 //  NULL but compiled code
                                                 //  still null-checks)
        p->refcount += 1;                         // <-- BUG: extra refcount++
                                                  //     leaves p at refcount=2
                                                  //     with nothing to match
    }
    return p;
}
```

The two-argument overload has the same shape, with the extra
`refcount++` in the same position. **The patch removes that one
line from each function.** After the patch:

```c
Palette* Palette::makeModifiedPalette() {
    Palette* p = (Palette*)operator new(0x48);
    if (p == NULL) return NULL;

    p = Palette::Palette(p, 0x800);              // ctor sets p->refcount = 1
    if (p != NULL) {
        // (the buggy p->refcount += 1 is removed)
    }
    return p;
}
```

That's the whole change. Two `refcount++` statements deleted from
two functions — one C statement each. The reference counting model
and every other line of code in the program is left exactly as the
original developers wrote it.

For reference, the raw Ghidra decompile of the EoR no-arg overload,
unmodified except for layout:

```c
int FUN_0053efe0(void) {
    int iVar1 = FUN_005df0f5(0x48);              // operator new(0x48)
    if (iVar1 != 0) {
        iVar1 = FUN_0053ee60(0x800);              // ctor, returns this
        if (iVar1 != 0) {
            *(int *)(iVar1 + 0x24) =              // <-- BUG: refcount += 1
                *(int *)(iVar1 + 0x24) + 1;       //     at offset +0x24
        }
        return iVar1;
    }
    return 0;
}
```

And the matching 2013 PDB-symbolicated version, identical in structure:

```c
class Palette* Palette::makeModifiedPalette() {
    void* eax = operator new(0x48);
    if (eax == 0) return 0;
    void* result = Palette::Palette(eax, 0x800);
    if (result != 0)
        *(uint32_t*)((char*)result + 0x24) += 1;  // <-- BUG
    return result;
}
```

In x86, `p->refcount += 1` where `refcount` is at object offset
`+0x24` compiles to a 3-byte `inc dword [reg+0x24]` (encoded as
`FF 40 24` or `FF 46 24` depending on whether the compiler picked
EAX or ESI). Three bytes of NOP (`90 90 90`) take its place. Same
instruction-pointer flow, just one operation deleted.

## Verifying the patch

| File | Size (bytes) | SHA-256 |
|------|--------------|---------|
| Input (EoR retail) | 4,841,472 | `bca95bbebed4b9ed1ff09d0da83144e2fc4208f63ad7ada5cb47c3ca207ccba9` |
| Patched | 4,841,472 | `69ac75174a0ea0f5a1fcd1c17bad2a562fad6439e984f05ff103a44e02bf4fc1` |

Sizes are identical — the patch only flips six bytes in place. The
patcher script verifies both hashes; if either fails, it errors out
without changing anything.

## Effectiveness

Measured on a fleet of 15 EoR clients running side-by-side, patched
vs unpatched, with a custom diagnostic that counts live `Palette`
instances every 5 minutes:

| | Per-client palette allocation rate |
|---|---|
| **Unpatched** | 18-60 / minute under active play |
| **Patched**   | -4 to +5 / minute (net oscillates around zero) |

The patched fleet stays in equilibrium: new modified palettes get
created and destroyed at matched rates. Pre-patch, every new modified
palette was permanently retained.

Some patched clients show **net-decreasing** counts over time as
already-leaked palettes from before the patch finally hit `refcount=0`
and get destroyed by the existing cleanup chain.

A separate, smaller leak source (a `D3DXMesh`-family class, ~1,700
instances in the same dump) was identified but is **not** addressed by
this patch. Estimating its share at ~40-55% of the total leak budget,
the Palette fix alone reduces overall memory leak rate by roughly
**40-50%**, which should be enough to push the 5-day-uptime crash well
out of normal play sessions.

## How to apply

### Option 1: Patch the binary on disk (persistent)

1. Make sure you have Python 3 (no third-party packages needed)
2. Close all running `acclient.exe` processes
3. Run the patcher against your installed `acclient.exe`:

   ```
   python patch_acclient_eor.py "C:\Turbine\Asheron's Call\acclient.exe"
   ```

   This writes two files next to it:
   - `acclient.eor.orig.exe` — unmodified backup (just a copy)
   - `acclient.eor.patched.exe` — the patched binary
4. Replace `acclient.exe` with `acclient.eor.patched.exe`
5. Launch as normal

### Option 2: Patch already-running clients (no restart needed)

For testing the fix without swapping the binary, or for applying it
to long-running clients without losing your session:

```
python patch_acclient_eor_runtime.py
```

With no arguments, it auto-discovers every running `acclient.exe`
and patches them all in place via `WriteProcessMemory`. Useful flags:

- `--pid <N>` — patch a specific process only
- `--revert` — restore the original bytes
- `--dry-run` — report what would change without writing

The runtime patcher applies the exact same six bytes as the on-disk
patcher; it just writes them into the running process's code pages
instead of the file. It's idempotent and verifies the existing bytes
before writing, so re-running it on an already-patched process is a
no-op.

The runtime patch is **non-persistent** — once the process exits,
the next launch of `acclient.exe` reverts to unpatched. For a
permanent fix, use Option 1.

## How to revert

- On-disk: replace `acclient.exe` with `acclient.eor.orig.exe`. Or
  recompute the patch — it's reproducible from any clean copy of the
  EoR `acclient.exe`.
- Runtime: `python patch_acclient_eor_runtime.py --revert` (or just
  let the process exit).

## Why was this missed for so long?

The bug is in both the 2013 and 2017 (EoR) builds at the same
structural location. It probably leaked in 2013 too, but typical play
sessions back then were short enough that no one hit the 2 GB ceiling.
EoR clients running multi-day uptimes (idle camping, harvesting,
multi-boxing) finally exposed it.

## Discovery method

1. Took a procdump of a 27-hour-uptime EoR client
2. Enumerated leaked private RW regions in the 256-512 KB band (2,490
   of them, the size of an expanded Palette ARGB buffer)
3. Custom diagnostic (`owner_vtable_scan.py` — not included here, but
   the approach is straightforward) scans all committed memory for any
   DWORD pointing into those regions and walks backwards looking for a
   vtable. Top hit by far was vtable `0x007caa08`.
4. Matched that vtable against the symbolicated 2013 client (full PDB
   available) by signature — confirmed `Palette`.
5. Decompiled the constructor and the factory methods. The extra
   `refcount++` was visible in both 2013 and EoR pseudo-C.
6. Counted live instances in the dump: 56,664 palettes, 99.7% at
   `refcount=1, m_pMaintainer=NULL` — the precise shape of "started
   at 2, got released once, sits forever".
7. Verified by NOPing the increment in a few running clients via
   `WriteProcessMemory` and watching the allocation rate drop from
   18-60/min to ~0/min.

## Compatibility

- Works only on the EoR retail `acclient.exe` (size 4,841,472,
  hash above). Patcher refuses to touch anything else.
- Same binary format, same code path, same crash signatures (if any
  other bug occurs, it'll look identical to the unpatched client's
  crash for that bug)
- Does not affect plugins, Decal, or anything else that hooks the
  client externally

## Appendix: byte-level diff (for reproducibility)

Two 3-byte windows in the `.text` section change. Listed here only
for byte-exact reproducibility — the human meaning of the patch is
in the "What the patch does (as code)" section above, not here.

| File offset | VA | Function | Instruction | Original | Patched |
|---|---|---|---|---|---|
| `0x13EFFE` | `0x0053EFFE` | `Palette::makeModifiedPalette()` | `inc dword [eax+0x24]` | `FF 40 24` | `90 90 90` |
| `0x13F19C` | `0x0053F19C` | `Palette::makeModifiedPalette(DataID, Subpalette*)` | `inc dword [esi+0x24]` | `FF 46 24` | `90 90 90` |

(`+0x24` is the byte offset of the `refcount` field inside the
inherited `DBObj` layout — set to 1 by `DBObj::DBObj` and incremented
by `DBObj::AddRef`. Hand-verifiable from the 2013 PDB symbols.)

## License

The patcher script and this README are released under the MIT license
(see `LICENSE`). The patched binary itself is a derivative of
Turbine's `acclient.exe` and inherits its copyright — this repo does
not redistribute the binary, only the script needed to produce it
from a copy you already own.
