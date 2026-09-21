"""Merge a chain of consecutive .delta artifacts into one.

A `.delta` is a gzip'd tar holding `metadata.json`, the replacement files under `new_files/`
and one xdelta (VCDIFF) patch per modified payload member under `patches/`. Consecutive
releases chain in the decompressed domain: `new_meta.decompressed_sha256` of one delta equals
`old_meta.decompressed_sha256` of the next. `xdelta3 merge` composes the patches without the
source data, so a chain collapses into a single artifact and therefore a single install.

Integrity: the merged artifact is not an official GitHub asset and its own hash is a transport
check only. Authenticity rests on `new_meta.decompressed_sha256`, which is copied verbatim from
the official last delta of the chain and which the scooter verifies against the applied result.
The ESP cross-checks that value against GitHub over TLS before it accepts a merged bundle.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import zlib

_LOGGER = logging.getLogger(__name__)

META_NAME = "metadata.json"
# Spans up to MERGE_MAX_STEPS are built by composing the official per-release patches (cheap:
# ~1.3 MB per nightly to download, seconds of CPU). Longer spans are built as a FRESH delta
# between the two full images instead — see fresh_delta(): memory bounded by the encoder's
# source window regardless of span, and the result is smaller than a composed patch because it
# carries only the net change (measured 0823->0908, 23 nightlies: composed 14.55 MB vs fresh
# 10.83 MB) — at the price of downloading both full images (~330 MB) and ~1.4 GB of temp disk.
MERGE_MAX_STEPS = 15
MERGE_GROUP = 8  # merge in a tree of at most this many inputs per xdelta3 call
MAX_CHAIN = 64  # sanity cap on downloads; the real limit is memory, see check_budget()
# xdelta3 merge holds every input and the composed patch in memory at once. Measured on MDB
# nightlies: 5 / 12 / 23 steps with 7.3 / 10.8 / 30.1 MB of .delta input peaked at 353 / 527 /
# 1112 MB RSS on macOS (37-49x the compressed input bytes) and 36x on the Alpine build inside the
# Home Assistant container. Merging in a tree only trims what is held at once (23 steps in
# groups of 4: 817 MB) — the span-wide patch of the last merge dominates — so the budget is
# checked against the host's free memory instead of a fixed step count.
RSS_PER_INPUT_BYTE = 45
MEMORY_HEADROOM = 0.7  # never plan to use more than this share of what is available
MERGE_TIMEOUT_S = 900
# The official deltas are encoded with lzma secondary compression and a 64 MB source window
# (read off their VCDIFF header); a fresh delta uses the same so it applies and sizes alike.
ENCODE_ARGS = ["-S", "lzma", "-B", "67108864"]
ENCODE_TIMEOUT_S = 1800
# Peak RSS of the encoder measured at 226 MB for a 512 MiB payload with the settings above.
ENCODE_RSS_BYTES = 300_000_000


class ChainError(Exception):
    """The chain cannot be merged; the caller should fall back to serial installs."""


def xdelta3_path() -> str | None:
    """The bundled binary if present, else whatever is on PATH."""
    bundled = os.path.join(os.path.dirname(__file__), "bin", "xdelta3")
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    return shutil.which("xdelta3")


def available() -> bool:
    return xdelta3_path() is not None


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None  # cgroup v2 writes "max" for unlimited


def memory_available() -> int | None:
    """Bytes a merge may reasonably claim: MemAvailable, capped by a cgroup limit if there is one.

    None when the host does not expose the numbers (not Linux), in which case the caller does
    not gate on memory at all.
    """
    avail = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    avail = int(line.split()[1]) * 1024
                    break
    except OSError:
        return None
    if avail is None:
        return None
    # cgroup v2, then v1 — a container limit below the host's free memory is the real ceiling.
    limit, used = _read_int("/sys/fs/cgroup/memory.max"), _read_int("/sys/fs/cgroup/memory.current")
    if limit is None:
        limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        used = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        if limit is not None and limit >= 1 << 60:  # v1 reports "unlimited" as a huge number
            limit = None
    if limit is not None and used is not None:
        avail = min(avail, max(limit - used, 0))
    return avail


def check_budget(input_bytes: int, steps: int) -> None:
    """Refuse a merge the host cannot hold in memory, before anything is downloaded."""
    if steps > MAX_CHAIN:
        raise ChainError(f"chain of {steps} exceeds the limit of {MAX_CHAIN}")
    need = input_bytes * RSS_PER_INPUT_BYTE
    avail = memory_available()
    if avail is None:
        _LOGGER.debug("delta chain: host memory unknown, merging %d steps unchecked", steps)
        return
    if need > avail * MEMORY_HEADROOM:
        raise ChainError(
            f"merging {steps} steps ({input_bytes / 1e6:.1f} MB of patches) needs about "
            f"{need / 1e6:.0f} MB of RAM, the host has {avail / 1e6:.0f} MB available — "
            f"pin a nearer target to chain in two hops"
        )
    _LOGGER.debug("delta chain: %d steps need ~%d MB of %d MB available", steps, need // 1_000_000,
                  avail // 1_000_000)


def _member_name(name: str) -> str:
    """Normalise './patches/x' and 'patches/x' to the same key."""
    return name[2:] if name.startswith("./") else name


def _extract(path: str, dest: str) -> dict:
    """Unpack one delta into dest. Returns its metadata."""
    meta: dict | None = None
    with tarfile.open(path, "r:gz") as tar:
        for member in tar:
            name = _member_name(member.name)
            if not name or name.startswith("@PaxHeader") or "@PaxHeader" in name:
                continue
            if not member.isfile():
                continue
            if os.path.isabs(name) or ".." in name.split("/"):
                raise ChainError(f"unsafe member name in {os.path.basename(path)}: {member.name}")
            src = tar.extractfile(member)
            if src is None:
                continue
            if name == META_NAME:
                raw = src.read()
                meta = json.loads(raw)
                continue
            out = os.path.join(dest, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as fh:
                shutil.copyfileobj(src, fh)
    if meta is None:
        raise ChainError(f"{os.path.basename(path)} has no {META_NAME}")
    return meta


def _patched_members(meta: dict) -> dict:
    """The `changes` entries that carry an xdelta patch."""
    return {k: v for k, v in meta.get("changes", {}).items() if v.get("patch")}


def _check_link(prev: dict, nxt: dict) -> None:
    """Both artifacts must be adjacent and agree on the intermediate image."""
    if prev.get("new_artifact_name") != nxt.get("old_artifact_name"):
        raise ChainError(
            f"not adjacent: {prev.get('new_artifact_name')} != {nxt.get('old_artifact_name')}"
        )
    if prev.get("new_payload_checksum") != nxt.get("old_payload_checksum"):
        raise ChainError("payload checksums do not chain")
    a, b = _patched_members(prev), _patched_members(nxt)
    if set(a) != set(b):
        raise ChainError(f"patched members differ: {sorted(a)} vs {sorted(b)}")
    for key in a:
        if a[key]["new_meta"]["decompressed_sha256"] != b[key]["old_meta"]["decompressed_sha256"]:
            raise ChainError(f"'{key}' does not chain in the decompressed domain")


def merge(delta_paths: list[str], out_path: str) -> dict:
    """Merge consecutive deltas into out_path. Returns the merged metadata.

    delta_paths must be ordered oldest first. Raises ChainError on anything that does not
    compose cleanly, so the caller can fall back to installing the chain one step at a time.
    """
    if len(delta_paths) < 2:
        raise ChainError("a chain needs at least two deltas")
    # The listener checks the budget from the release listing before downloading; this is the
    # backstop for callers that hand over files directly.
    check_budget(sum(os.path.getsize(p) for p in delta_paths), len(delta_paths))
    exe = xdelta3_path()
    if exe is None:
        raise ChainError("xdelta3 is not available")

    with tempfile.TemporaryDirectory(prefix="lsc-chain-") as work:
        dirs, metas = [], []
        for idx, path in enumerate(delta_paths):
            dest = os.path.join(work, f"d{idx}")
            os.makedirs(dest)
            metas.append(_extract(path, dest))
            dirs.append(dest)
        for prev, nxt in zip(metas, metas[1:]):
            _check_link(prev, nxt)

        first, last = metas[0], metas[-1]
        merged_dir = os.path.join(work, "merged")
        os.makedirs(merged_dir)

        # Replacement files come from the last delta: they are the final versions.
        last_dir = dirs[-1]
        for root, _, files in os.walk(os.path.join(last_dir, "new_files")):
            for fname in files:
                src = os.path.join(root, fname)
                rel = os.path.relpath(src, last_dir)
                dst = os.path.join(merged_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)

        # One composed patch per modified member.
        changes = json.loads(json.dumps(last["changes"]))
        for key, entry in _patched_members(last).items():
            patch_rel = os.path.join("patches", entry["patch"])
            inputs = [os.path.join(d, "patches", _patched_members(m)[key]["patch"])
                      for d, m in zip(dirs, metas)]
            for src in inputs:
                if not os.path.isfile(src):
                    raise ChainError(f"missing patch file for '{key}'")
            out = os.path.join(merged_dir, patch_rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            # xdelta3 merge -m <older> [-m <next>] <newest> <output>
            cmd = [exe, "merge"]
            for src in inputs[:-1]:
                cmd += ["-m", src]
            cmd += [inputs[-1], out]
            proc = subprocess.run(cmd, capture_output=True, timeout=MERGE_TIMEOUT_S, check=False)
            if proc.returncode != 0 or not os.path.isfile(out):
                raise ChainError(
                    f"xdelta3 merge failed for '{key}': {proc.stderr.decode('utf-8', 'replace')[:300]}"
                )
            # The composed patch spans the whole chain, so the old side is the chain's start.
            changes[key]["old_meta"] = json.loads(
                json.dumps(_patched_members(first)[key]["old_meta"])
            )

        meta = json.loads(json.dumps(last))
        meta["old_artifact_name"] = first["old_artifact_name"]
        meta["old_payload_checksum"] = first["old_payload_checksum"]
        meta["changes"] = changes
        meta["merged_from"] = [m["old_artifact_name"] for m in metas] + [last["new_artifact_name"]]

        with open(os.path.join(merged_dir, META_NAME), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)

        _pack(merged_dir, out_path)
    return meta


def merge_tree(delta_paths: list[str], out_path: str, work: str, group: int = MERGE_GROUP) -> dict:
    """merge() in a tree of at most `group` inputs per call.

    Composing in stages lowers the peak by the inputs that would otherwise be held at once
    (measured: 23 steps flat 1112 MB, in groups of 4 817 MB). It does not change the last
    step, which must still hold the span-wide patch — that is why long spans go to
    fresh_delta() instead. Intermediates live under `work` and are removed afterwards.
    """
    if len(delta_paths) <= group:
        return merge(delta_paths, out_path)
    tmp = tempfile.mkdtemp(prefix="lsc-tree-", dir=work)
    try:
        level, depth = list(delta_paths), 0
        while len(level) > group:
            nxt = []
            for i in range(0, len(level), group):
                part = level[i : i + group]
                if len(part) == 1:
                    nxt.append(part[0])
                    continue
                out = os.path.join(tmp, f"l{depth}-{i}.delta")
                merge(part, out)
                nxt.append(out)
            level, depth = nxt, depth + 1
        return merge(level, out_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _payload_from_mender(mender_path: str, member: str, dest: str) -> tuple[dict, str]:
    """Stream one compressed payload member of a .mender to `dest`, decompressed.

    Returns (meta, payload_checksum): meta in the shape of a delta's old_meta/new_meta for that
    member, and the ext4 checksum the artifact's own manifest states for it (what the delta
    format calls the payload checksum). Nothing is held in memory beyond one block.
    """
    meta: dict | None = None
    checksum = ""
    with tarfile.open(mender_path, "r|") as tar:  # stream mode: no seeking through 166 MB
        for entry in tar:
            name = _member_name(entry.name)
            src = tar.extractfile(entry) if entry.isfile() else None
            if src is None:
                continue
            if name == "manifest":
                for line in src.read().decode("utf-8", "replace").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1].startswith("data/"):
                        checksum = parts[0]
                        break
                continue
            if name != member:
                continue
            comp_h, raw_h = hashlib.sha256(), hashlib.sha256()
            comp_n = raw_n = 0
            inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
            with open(dest, "wb") as out:
                for block in iter(lambda: src.read(1 << 20), b""):
                    comp_h.update(block)
                    comp_n += len(block)
                    raw = inflate.decompress(block)
                    raw_h.update(raw)
                    raw_n += len(raw)
                    out.write(raw)
                raw = inflate.flush()
                raw_h.update(raw)
                raw_n += len(raw)
                out.write(raw)
            meta = {
                "original_name": os.path.basename(member),
                "compressed": True,
                "sha256": comp_h.hexdigest(),
                "size": comp_n,
                "decompressed_sha256": raw_h.hexdigest(),
                "decompressed_size": raw_n,
            }
    if meta is None:
        raise ChainError(f"{os.path.basename(mender_path)} has no {member}")
    if not checksum:
        raise ChainError(f"{os.path.basename(mender_path)} has no payload checksum in its manifest")
    return meta, checksum


def fresh_delta(base_mender: str, target_mender: str, target_delta: str, from_tag: str,
                out_path: str, work: str) -> dict:
    """Encode a new delta straight from the two full images.

    The official target delta supplies everything that is not the patch: the replacement
    files (header.tar.gz, manifest), `new_meta` and the new payload checksum — so what the
    scooter verifies after applying is exactly what the official artifact would have it verify.
    Our own unpacking of the target image is cross-checked against that `new_meta` before
    anything is encoded.
    """
    exe = xdelta3_path()
    if exe is None:
        raise ChainError("xdelta3 is not available")
    tmp = tempfile.mkdtemp(prefix="lsc-fresh-", dir=work)
    try:
        ref_dir = os.path.join(tmp, "ref")
        os.makedirs(ref_dir)
        ref = _extract(target_delta, ref_dir)
        patched = _patched_members(ref)
        if len(patched) != 1:
            raise ChainError(f"expected one patched member, got {sorted(patched)}")
        member, entry = next(iter(patched.items()))

        # File names as in the official VCDIFF application header; xdelta3 records them.
        old_raw = os.path.join(tmp, "old_" + os.path.basename(member) + ".decompressed")
        new_raw = os.path.join(tmp, "new_" + os.path.basename(member) + ".decompressed")
        old_meta, old_checksum = _payload_from_mender(base_mender, member, old_raw)
        new_meta, new_checksum = _payload_from_mender(target_mender, member, new_raw)
        for key in ("sha256", "size", "decompressed_sha256", "decompressed_size"):
            if new_meta[key] != entry["new_meta"].get(key):
                raise ChainError(f"target image does not match the official delta's new_meta ({key})")
        if new_checksum != ref.get("new_payload_checksum"):
            raise ChainError("target image payload checksum differs from the official delta")

        out_dir = os.path.join(tmp, "out")
        for root, _, files in os.walk(os.path.join(ref_dir, "new_files")):
            for fname in files:
                src = os.path.join(root, fname)
                dst = os.path.join(out_dir, os.path.relpath(src, ref_dir))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
        patch_out = os.path.join(out_dir, "patches", entry["patch"])
        os.makedirs(os.path.dirname(patch_out), exist_ok=True)
        cmd = [exe, "-e", "-f", *ENCODE_ARGS, "-s", os.path.basename(old_raw),
               os.path.basename(new_raw), patch_out]
        proc = subprocess.run(cmd, capture_output=True, timeout=ENCODE_TIMEOUT_S, check=False,
                              cwd=tmp)
        if proc.returncode != 0 or not os.path.isfile(patch_out):
            raise ChainError(f"xdelta3 encode failed: {proc.stderr.decode('utf-8', 'replace')[:300]}")
        os.remove(old_raw)
        os.remove(new_raw)

        meta = json.loads(json.dumps(ref))
        meta["old_artifact_name"] = f"release-{from_tag}"
        meta["old_payload_checksum"] = old_checksum
        meta["changes"][member]["old_meta"] = old_meta
        meta.pop("merged_from", None)
        meta["generated_from"] = [meta["old_artifact_name"], ref["new_artifact_name"]]
        with open(os.path.join(out_dir, META_NAME), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        _pack(out_dir, out_path)
        return meta
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _pack(src_dir: str, out_path: str) -> None:
    """Repack in the layout update-service expects: metadata first, then the rest.

    Deterministic: fixed mtimes and a zeroed gzip header, so rebuilding the same span (after a
    Home Assistant restart, say) yields a byte-identical bundle. The scooter resumes a partial
    only when the bundle's SHA-256 in START matches, so a rebuilt-but-different file would
    throw away hours of BLE transfer.
    """
    names = [META_NAME]
    for root, _, files in os.walk(src_dir):
        for fname in sorted(files):
            rel = os.path.relpath(os.path.join(root, fname), src_dir)
            if rel != META_NAME:
                names.append(rel)
    tmp = out_path + ".part"
    with open(tmp, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tar:
        for rel in names:
            full = os.path.join(src_dir, rel)
            info = tar.gettarinfo(full, arcname="./" + rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            with open(full, "rb") as fh:
                tar.addfile(info, fh)
    os.replace(tmp, out_path)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def result_hashes(meta: dict) -> dict:
    """The applied-result hashes, which are what authenticity rests on."""
    return {
        key: entry["new_meta"]["decompressed_sha256"]
        for key, entry in _patched_members(meta).items()
    }
