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

## What does the patch do?

It NOPs the buggy `inc dword [reg+0x24]` instruction at the end of
both `makeModifiedPalette` overloads:

| File offset | Function | Original | Patched |
|-------------|----------|---------|---------|
| `0x13EFFE` | `Palette::makeModifiedPalette()` | `FF 40 24` | `90 90 90` |
| `0x13F19C` | `Palette::makeModifiedPalette(DataID, Subpalette*)` | `FF 46 24` | `90 90 90` |

Six bytes total. The function still returns the new palette
pointer — but now at `refcount = 1` like every other constructor.
The existing release path then brings it to 0 and destroys it
correctly when no longer used. The reference counting and all other
logic in the program is untouched.

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

## How to revert

Replace `acclient.exe` with `acclient.eor.orig.exe`. Or recompute the
patch — it's reproducible from any clean copy of the EoR `acclient.exe`.

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

## License

The patcher script and this README are released under the MIT license
(see `LICENSE`). The patched binary itself is a derivative of
Turbine's `acclient.exe` and inherits its copyright — this repo does
not redistribute the binary, only the script needed to produce it
from a copy you already own.
