# Patch packs - RetroRemakes PS3 Studio

RetroRemakes PS3 Studio is a RetroRemakes product.

A **patch pack** is a set of fixes for one game: for example a port's folder of 200+ numbered ps3recomp patches. When a game matches a pack, the Studio:

1. makes **its own copy** of the ps3recomp backend at the revision the pack names (your shared backend and every other game are untouched),
2. applies the pack's backend patches **in file-name order** (`0001-...`, `0002-...`, ...),
3. builds the game against that copy, copies the pack's project files and applies its project patches to the generated port,
4. sets the pack's environment variables when you test the native build.

The patched copy is cached, so it is only prepared once per pack version. A pack never contains game code or data; it is only your fixes.

## Using a pack

1. Open the **Patch packs** tab.
2. **Add pack (folder)…** or **Add pack (ZIP)…** and pick what the pack author gave you.
3. Build the game as usual. The build log shows `Using patch pack: <name>`.

Games pick a matching pack automatically by title ID or exact EBOOT hash. Under "Use a pack for one game" you can force a pack, or turn packs off, for a single game.

## Community library

The **Community library** in the Patch packs tab lists every pack shared by players at https://github.com/RemakerGamee/PS3-Studio-Packs, and shows which of your games each one is for.

- **Stays in sync by itself:** a few seconds after the Studio starts, and every 6 hours while it is open. Press **Sync now** any time.
- **Installs automatically:** by default, new packs for games in your library. You can choose every pack, or updates only.
- **Updates automatically:** when an author releases a new version, packs you got from the library are updated on the next sync.
- **Your own packs are safe:** a pack you added or made yourself is never replaced by a community pack with the same id.
- Every download is checked against the checksum in the community list before it is installed. Turn off "Keep in sync automatically" if you prefer to install by hand.

To share your own pack: select it, press **Share selected pack…**, upload the ZIP to a release on your GitHub, and add the entry it gives you to the community list with a pull request. The steps are in the community repository's CONTRIBUTING.md (button **How to add your pack**). Once it is accepted, everyone's Studio picks it up on its next sync.

## Making a pack the quick way

Already have a folder of numbered `.patch` files (made with `git diff` or `git format-patch` against ps3recomp)?

1. **Patch packs > Make pack from a patch folder…** and choose that folder.
2. Enter a name, the game's **title IDs** (all regions you support, for example `BCUS98234, BCES01007`), and the **ps3recomp commit or tag** your patches were made against.
3. Press **Make pack**. The pack is installed and appears in the list. Share the folder from **Open packs folder** (zip it up).

## The pack format

```
my-pack/
  pack.json
  patches/        backend patches, applied in file-name order
    0001-something.patch
    0002-something-else.patch
  project/        (optional) files copied over the generated port/ folder
  project-patches/  (optional) patches applied to the generated port/ folder
```

`pack.json`:

```json
{
  "schema": 1,
  "id": "kz3-example",
  "name": "Killzone 3 example pack",
  "version": "1.0.0",
  "author": "Your name",
  "homepage": "https://github.com/you/your-port",
  "license": "MIT",
  "description": "What this pack fixes.",
  "games": {
    "title_ids": ["BCUS98234", "BCES01007"],
    "elf_sha256": []
  },
  "backend": {
    "revision": "v0.18.0",
    "repository": "https://github.com/sp00nznet/ps3recomp.git",
    "patches": ["patches/*.patch"],
    "strip": 1,
    "studio_fixes": false
  },
  "project": {
    "files": "project",
    "patches": ["project-patches/*.patch"]
  },
  "env": {
    "EXAMPLE_SETTING": "1"
  }
}
```

| Field | Meaning |
|---|---|
| `id` | Short unique name: lowercase letters, digits, `-`. |
| `games.title_ids` | PS3 title IDs this pack is for. The game's `PARAM.SFO` is checked. |
| `games.elf_sha256` | Optional exact EBOOT.elf hashes, for packs that only work with one game version. |
| `backend.revision` | ps3recomp commit, tag or branch the patches were made against. Blank = the Studio's pinned revision. |
| `backend.repository` | Where to download that revision if it is not already present. Default: the official ps3recomp repository. A fork works too. |
| `backend.patches` | Patch files (glob patterns), applied in file-name order. |
| `backend.strip` | Leading path parts to strip, like `patch -p1`. Default 1 (`a/` and `b/` prefixes). |
| `backend.studio_fixes` | Apply RetroRemakes' own backend fixes first. Default: on only when no revision is given. Leave off for patch sets made against plain ps3recomp. |
| `project.files` | A folder copied over the generated `port/` project (custom CMakeLists.txt, HLE files, configs...). |
| `project.patches` | Patches applied to the generated `port/` project. |
| `env` | Environment variables set when the native build is tested. |

Patches are applied with `git apply`. If a patch needs fuzz, the Studio falls back to `patch` (included with Git for Windows). If a patch still does not fit, the build stops and names the patch, and the full log is next to the prepared backend in `workspace/tools/patch-pack-backends/`.

## Good practice for pack authors

- Pin `backend.revision` to an exact commit or tag, so everyone gets the same result.
- Keep patch numbers stable, and add new patches with new numbers.
- Raise `version` with each release. A changed pack automatically gets a fresh backend copy.
- Include your license and credits in the pack folder.
- Never put game files, decrypted EBOOTs, firmware or keys in a pack.
