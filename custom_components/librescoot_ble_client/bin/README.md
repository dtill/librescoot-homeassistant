# Bundled xdelta3

Used by the relay to merge consecutive `.delta` patches (`chain.py`). Home Assistant runs on
Alpine Linux (musl) and ships only for **amd64** and **aarch64**; `chain.xdelta3_path()` picks the
binary for `platform.machine()` and runs `-V` once at start-up. A binary that does not run there
makes chaining report "not available" instead of failing mid-build.

Both files are the unmodified `usr/bin/xdelta3` from the official Alpine package, verified against
the Alpine signing keys (index signature → package checksum → package signature → data hash):

| File | Alpine package | Package checksum (index `C:`) | SHA-256 of the binary |
| :--- | :--- | :--- | :--- |
| `xdelta3-x86_64` | v3.24 community `xdelta3-3.1.0-r2` x86_64 | `Q1Tr7Gg9BF3C3AFFYXQBiavZ60lew=` | `7571d6bc4162a7017f5344bd6533a56fa476af5f542d7eeb10d9a4b4673d89c9` |
| `xdelta3-aarch64` | v3.24 community `xdelta3-3.1.0-r2` aarch64 | `Q1txoKkj4aJxuRIEYjWyP0BHBh5oU=` | `9785700a3bbeb820902a1149988ed81439b0e553b3b028ef834d1af88db59158` |

Runtime dependencies: `libc.musl-<arch>.so.1` and `liblzma.so.5`, both part of the Home Assistant
core image. License: GPL-2.0-or-later (as declared by the Alpine package); source:
https://github.com/jmacd/xdelta and the Alpine aports `community/xdelta3` recipe.
