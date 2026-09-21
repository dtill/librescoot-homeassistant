"""Plain-HTTP OTA byte listener.

Runs its own aiohttp server on a dedicated port. HA Core uses host networking on HA OS /
Supervised, so this port is reachable on the LAN — which means it works even when HA itself
is HTTPS-only (the classic ESP cannot do the RSA-4096 GitHub-CDN TLS handshake, so it must
fetch the bulk bytes over plain HTTP).

It is a transparent Range proxy to the public GitHub release download URL: HA does the TLS to
GitHub, the ESP sees plain HTTP with byte-range support (needed for resume / go-back-N).

Security: access is gated by a per-device secret embedded in the URL path. The bytes are
public firmware and their integrity is guaranteed by the SHA-256 the scooter verifies from
the BLE START message, so the transport needs no confidentiality — a bearer secret over
plain HTTP on the LAN is enough, and it costs the ESP zero crypto (it just does a GET of the
URL the integration hands it).
"""
from __future__ import annotations

import time

import asyncio
import io
import json
import logging
import os
import re
import shutil
import tarfile

from aiohttp import ClientTimeout, web

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import chain as chain_mod

_LOGGER = logging.getLogger(__name__)

# Headers copied straight through from the GitHub response to the ESP.
# The release listing changes at most a few times a day; serving it from cache keeps every
# device on the relay from hitting the GitHub API separately.
_CHECK_TTL = 300.0

_PASS = ("Content-Length", "Content-Range", "Accept-Ranges", "Content-Type", "ETag", "Last-Modified")

# Merged chains are deterministic for a (component, from, to) triple, so they are cached on disk.
CHAIN_DIR = "librescoot_ota_chain"
_CHAIN_KEEP = 4
_CHAIN_ERROR_TTL_S = 300  # a refused span is answered from memory this long before a retry
_FULL_IMAGE_BYTES = 170_000_000  # a full MDB/DBC .mender, for the disk check of a fresh delta
_PAYLOAD_BYTES = 540_000_000  # its data/0000.tar.gz unpacked (512 MiB)
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class OtaProxy:
    """One shared listener; each configured device registers a secret -> (repo, token)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._runner: web.AppRunner | None = None
        self._port: int | None = None
        self._secrets: dict[str, dict] = {}
        self._scans: dict[str, str] = {}  # secret -> last posted "MAC@rssi,…" scan result
        self._bases: dict[str, str] = {}  # "tag/filename" -> delta base tag (immutable, cached)
        self._notes: dict[str, str] = {}  # tag -> release body (immutable once published)
        self._checks: dict[str, tuple[float, str]] = {}  # channel -> (fetched_at, body)
        self._chains: dict[str, dict] = {}  # "comp/from/to" -> merged-bundle facts
        self._chain_lock = asyncio.Lock()   # one build at a time: a merge peaks at ~1 GB RSS
        self._chain_jobs: dict[str, asyncio.Task] = {}  # key -> running build
        self._chain_phase: dict[str, str] = {}  # key -> what the running build is doing
        self._chain_errors: dict[str, tuple[float, str]] = {}  # key -> (when, reason)

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def empty(self) -> bool:
        return not self._secrets

    def register(self, secret: str, repo: str, token: str | None) -> None:
        """`token` is stored for completeness but intentionally NOT used for the byte download —
        see `_handle`: public release assets need none, and it must not follow the CDN redirect."""
        self._secrets[secret] = {"repo": repo, "token": token}

    def unregister(self, secret: str) -> None:
        self._secrets.pop(secret, None)
        self._scans.pop(secret, None)

    def get_scan(self, secret: str) -> str:
        """The scooter list the ESP last POSTed for this device (empty if none yet)."""
        return self._scans.get(secret, "")

    def clear_scan(self, secret: str) -> None:
        self._scans.pop(secret, None)

    async def start(self, port: int) -> bool:
        if self._runner is not None:
            return True
        app = web.Application()
        app.router.add_route("GET", "/ota/{secret}/{tag}/{filename}", self._handle)
        app.router.add_route("HEAD", "/ota/{secret}/{tag}/{filename}", self._handle)
        # The ESP POSTs its BLE-scan result here (so there's no HA entity for the raw list).
        app.router.add_route("POST", "/scan/{secret}", self._handle_scan)
        # Which release a .delta patches against — read here so a board without the memory to
        # decompress the archive itself still gets the check.
        app.router.add_route("GET", "/base/{secret}/{tag}/{filename}", self._handle_base)
        app.router.add_route("GET", "/notes/{secret}/{tag}", self._handle_notes)
        app.router.add_route("GET", "/check/{secret}/{channel}", self._handle_check)
        app.router.add_route("GET", "/chain/{secret}/{component}/{from_tag}/{to_tag}",
                             self._handle_chain)
        app.router.add_route("GET", "/chainbytes/{secret}/{component}/{from_tag}/{to_tag}/{filename}",
                             self._handle_chain_bytes)
        app.router.add_route("HEAD", "/chainbytes/{secret}/{component}/{from_tag}/{to_tag}/{filename}",
                             self._handle_chain_bytes)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0", port).start()
        except OSError as err:
            await runner.cleanup()
            _LOGGER.error("OTA byte listener could not bind port %d: %s", port, err)
            return False
        self._runner = runner
        self._port = port
        _LOGGER.info("OTA byte listener on http://0.0.0.0:%d/ota/<secret>/<tag>/<file>", port)
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._port = None

    async def _handle_scan(self, request: web.Request) -> web.Response:
        secret = request.match_info["secret"]
        if secret not in self._secrets:
            raise web.HTTPForbidden(text="bad token")
        body = (await request.text())[:512]  # "MAC@rssi,MAC@rssi*,…"
        self._scans[secret] = body
        return web.Response(text="ok")

    async def _handle_base(self, request: web.Request) -> web.Response:
        """Return the release a .delta was generated against, as a bare tag.

        A .delta is a gzip'd tar whose metadata.json carries "old_artifact_name". The scooter never
        checks it — it assumes the running version is the base, applies the patch and only fails at
        the end — so the ESP refuses a mismatching delta up front. Boards with PSRAM read this from
        the archive themselves; the rest ask here.

        200 + tag when known, 204 when it could not be determined (the ESP then proceeds as before,
        an unreadable base is not evidence of a mismatch).
        """
        info = self._secrets.get(request.match_info["secret"])
        if info is None:
            raise web.HTTPForbidden(text="bad token")
        tag = request.match_info["tag"]
        filename = request.match_info["filename"]
        if ".." in tag or ".." in filename or "/" in tag or "/" in filename:
            raise web.HTTPBadRequest
        if not filename.endswith(".delta"):
            return web.Response(status=204)  # full images carry no base requirement

        key = f"{tag}/{filename}"
        if (cached := self._bases.get(key)) is not None:
            return web.Response(text=cached)

        gh = f"https://github.com/{info['repo']}/releases/download/{tag}/{filename}"
        session = async_get_clientsession(self._hass)
        timeout = ClientTimeout(total=30)
        try:
            # The archive head is enough: metadata.json is the first regular member.
            async with session.get(
                gh, headers={"Range": "bytes=0-16383"}, allow_redirects=True, timeout=timeout
            ) as up:
                if up.status not in (200, 206):
                    _LOGGER.warning("delta base: %s answered HTTP %s", gh, up.status)
                    return web.Response(status=204)
                head = await up.content.read(16384)
        except Exception as err:  # noqa: BLE001 - upstream/network failures are not fatal here
            _LOGGER.warning("delta base: could not fetch %s: %s", gh, err)
            return web.Response(status=204)

        base = await self._hass.async_add_executor_job(_delta_base_from_head, head)
        if not base:
            return web.Response(status=204)
        self._bases[key] = base
        return web.Response(text=base)

    async def _handle_check(self, request: web.Request) -> web.Response:
        """Return the channel's newest release and its tag list, as plain text.

        First line is the latest published release, the rest are the channel's tags, newest first:

            latest nightly-20260904T104640
            nightly-20260904T104640
            nightly-20260903T021411
            ...

        Lets a board without the RAM for a TLS session run its update check over the relay. The
        SHA-256 the scooter verifies against is not part of this — the ESP fetches that itself from
        api.github.com over TLS when an install starts.
        """
        info = self._secrets.get(request.match_info["secret"])
        if info is None:
            raise web.HTTPForbidden(text="bad token")
        channel = request.match_info["channel"]
        if not channel.isalnum():
            raise web.HTTPBadRequest

        cached = self._checks.get(channel)
        if cached is not None and (time.monotonic() - cached[0]) < _CHECK_TTL:
            return web.Response(text=cached[1], content_type="text/plain")

        api = f"https://api.github.com/repos/{info['repo']}/releases?per_page=100"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "librescoot-ble-client"}
        if info.get("token"):
            headers["Authorization"] = f"Bearer {info['token']}"  # api.github.com only, no redirect
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(api, headers=headers, timeout=ClientTimeout(total=30)) as up:
                if up.status != 200:
                    _LOGGER.warning("update check: %s answered HTTP %s", api, up.status)
                    return web.Response(status=204)
                releases = await up.json()
        except Exception as err:  # noqa: BLE001 - upstream/network failures are not fatal here
            _LOGGER.warning("update check: could not fetch %s: %s", api, err)
            return web.Response(status=204)

        prefix = f"{channel}-"
        tags = [r["tag_name"] for r in releases
                if not r.get("draft") and str(r.get("tag_name", "")).startswith(prefix)]
        if not tags:
            return web.Response(status=204)
        body = "latest " + max(tags) + "\n" + "\n".join(tags) + "\n"
        self._checks[channel] = (time.monotonic(), body)
        return web.Response(text=body, content_type="text/plain")

    async def _handle_notes(self, request: web.Request) -> web.Response:
        """Return a release's notes as plain text.

        Display text only. It has no part in verifying anything: the SHA-256 the scooter checks the
        firmware against is fetched by the ESP itself, straight from api.github.com over TLS, on
        every board. This exists because a board without PSRAM cannot afford a second TLS session
        beside BLE and WiFi, and the notes were the one thing large enough to make it fail.

        200 + body, or 204 when it could not be fetched (the ESP then shows the header alone).
        """
        info = self._secrets.get(request.match_info["secret"])
        if info is None:
            raise web.HTTPForbidden(text="bad token")
        tag = request.match_info["tag"]
        if ".." in tag or "/" in tag:
            raise web.HTTPBadRequest

        if (cached := self._notes.get(tag)) is not None:
            return web.Response(text=cached, content_type="text/plain")

        api = f"https://api.github.com/repos/{info['repo']}/releases/tags/{tag}"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "librescoot-ble-client"}
        if info.get("token"):
            headers["Authorization"] = f"Bearer {info['token']}"  # api.github.com only, no redirect
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(api, headers=headers, timeout=ClientTimeout(total=30)) as up:
                if up.status != 200:
                    _LOGGER.warning("release notes: %s answered HTTP %s", api, up.status)
                    return web.Response(status=204)
                body = (await up.json()).get("body") or ""
        except Exception as err:  # noqa: BLE001 - upstream/network failures are not fatal here
            _LOGGER.warning("release notes: could not fetch %s: %s", api, err)
            return web.Response(status=204)

        if not body:
            return web.Response(status=204)
        self._notes[tag] = body
        return web.Response(text=body, content_type="text/plain")

    # ---- delta chaining -------------------------------------------------------------
    #
    # Merging a run of consecutive deltas turns N installs and N scooter reboots into one. Up to
    # MERGE_MAX_STEPS the official patches are composed (xdelta3 merge); beyond that a fresh
    # delta is encoded from the two full images (chain.fresh_delta), which has no span limit
    # and is smaller, at the price of a ~330 MB download. Building runs in the background: the
    # ESP polls /chain and gets 503 with the current phase until the bundle is ready.
    #
    # Integrity: a merged bundle is not an official GitHub asset, so the SHA-256 the ESP puts in
    # START is a transport check only. Authenticity rests on "result" below — the applied-image
    # hash, copied verbatim from the official last delta — which the scooter verifies after the
    # patch run. A board that can read the official delta head itself cross-checks that value
    # against GitHub over TLS before accepting the bundle; a board that cannot is trusting this
    # host, and says so in its log.

    def _chain_dir(self) -> str:
        return self._hass.config.path(CHAIN_DIR)

    async def _handle_chain(self, request: web.Request) -> web.Response:
        """Describe a merged bundle as plain text, building it in the background first.

        200 with one `key value` per line once the bundle exists; 503 with `building <phase>`
        while it is being built (poll again); 422 with a one-line reason when the span cannot
        be bridged (the caller falls back to installing the steps one at a time and can show
        the reason); 204 when merging is not possible on this host at all.
        """
        info = self._secrets.get(request.match_info["secret"])
        if info is None:
            raise web.HTTPForbidden(text="bad token")
        comp = request.match_info["component"]
        from_tag = request.match_info["from_tag"]
        to_tag = request.match_info["to_tag"]
        if comp not in ("mdb", "dbc") or not _TAG_RE.match(from_tag) or not _TAG_RE.match(to_tag):
            raise web.HTTPBadRequest
        if not chain_mod.available():
            _LOGGER.warning("delta chain: xdelta3 is not available on this host")
            return web.Response(status=204)

        key = f"{comp}/{from_tag}/{to_tag}"
        if (facts := self._chains.get(key)) and os.path.isfile(facts["path"]):
            return web.Response(text=_chain_body(facts))
        if (job := self._chain_jobs.get(key)) and not job.done():
            return web.Response(status=503, text=f"building {self._chain_phase.get(key, '')}")
        if (err := self._chain_errors.get(key)) and time.monotonic() - err[0] < _CHAIN_ERROR_TTL_S:
            return web.Response(status=422, text=err[1])
        if any(not j.done() for j in self._chain_jobs.values()):
            return web.Response(status=503, text="building another bundle first")
        self._chain_errors.pop(key, None)
        self._chain_phase[key] = "queued"
        self._chain_jobs[key] = self._hass.async_create_background_task(
            self._chain_job(key, info, comp, from_tag, to_tag), f"librescoot chain {key}"
        )
        return web.Response(status=503, text="building queued")

    async def _chain_job(self, key: str, info: dict, comp: str, from_tag: str, to_tag: str) -> None:
        async with self._chain_lock:
            try:
                facts = await self._build_chain(key, info, comp, from_tag, to_tag)
            except chain_mod.ChainError as err:
                _LOGGER.warning("delta chain %s: %s", key, err)
                self._chain_errors[key] = (time.monotonic(), str(err))
            except Exception as err:  # noqa: BLE001 - never take the listener down over this
                _LOGGER.exception("delta chain %s failed: %s", key, err)
                self._chain_errors[key] = (time.monotonic(), f"build failed: {err}")
            else:
                self._chains[key] = facts
            finally:
                self._chain_phase.pop(key, None)

    async def _download(self, url: str, dest: str, key: str, label: str) -> None:
        """GitHub release asset to `dest`, reporting progress into the build phase."""
        if os.path.isfile(dest):
            return
        session = async_get_clientsession(self._hass)
        timeout = ClientTimeout(total=None, connect=30, sock_read=120)
        async with session.get(url, allow_redirects=True, timeout=timeout) as up:
            if up.status != 200:
                raise chain_mod.ChainError(f"{os.path.basename(dest)}: HTTP {up.status}")
            total = up.content_length or 0
            done = 0
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                async for block in up.content.iter_chunked(262144):
                    fh.write(block)
                    done += len(block)
                    if total:
                        self._chain_phase[key] = f"downloading {label} {done * 100 // total}%"
            os.replace(tmp, dest)

    async def _build_chain(self, key: str, info: dict, comp: str, from_tag: str, to_tag: str) -> dict:
        """Bridge from_tag -> to_tag with one .delta: composed for short spans, fresh for long."""
        self._chain_phase[key] = "listing releases"
        steps, sizes = await self._chain_steps(info, comp, from_tag, to_tag)
        if len(steps) < 2:
            raise chain_mod.ChainError(
                f"{len(steps)} step(s) between {from_tag} and {to_tag} — nothing to merge"
            )
        work = self._chain_dir()
        await self._hass.async_add_executor_job(_ensure_dir, work)
        out_name = f"librescoot-unu-{comp}-{to_tag}.delta"
        out_path = os.path.join(work, "merged", out_name)
        await self._hass.async_add_executor_job(_ensure_dir, os.path.dirname(out_path))
        base = f"https://github.com/{info['repo']}/releases/download"

        if len(steps) <= chain_mod.MERGE_MAX_STEPS:
            # Refuse what the host cannot hold before pulling tens of MB from GitHub for nothing.
            chain_mod.check_budget(sum(sizes), len(steps))
            paths = []
            for i, tag in enumerate(steps, 1):
                name = f"librescoot-unu-{comp}-{tag}.delta"
                dest = os.path.join(work, name)
                await self._download(f"{base}/{tag}/{name}", dest, key, f"step {i}/{len(steps)}")
                paths.append(dest)
            self._chain_phase[key] = f"merging {len(steps)} steps"
            meta = await self._hass.async_add_executor_job(chain_mod.merge_tree, paths, out_path, work)
            how = f"merged {len(steps)} steps"
        else:
            # Two full images plus their unpacked payloads: refuse when the disk cannot take it.
            need = 2 * _FULL_IMAGE_BYTES + 2 * _PAYLOAD_BYTES
            free = shutil.disk_usage(work).free
            if free < need:
                raise chain_mod.ChainError(
                    f"a fresh delta over {len(steps)} steps needs ~{need // 1_000_000} MB of "
                    f"free disk in {work}, {free // 1_000_000} MB left"
                )
            avail = chain_mod.memory_available()
            if avail is not None and chain_mod.ENCODE_RSS_BYTES > avail * chain_mod.MEMORY_HEADROOM:
                raise chain_mod.ChainError(
                    f"encoding a fresh delta needs ~{chain_mod.ENCODE_RSS_BYTES // 1_000_000} MB "
                    f"of RAM, the host has {avail // 1_000_000} MB available"
                )
            images = os.path.join(work, "images")
            await self._hass.async_add_executor_job(_ensure_dir, images)
            base_img = os.path.join(images, f"librescoot-unu-{comp}-{from_tag}.mender")
            target_img = os.path.join(images, f"librescoot-unu-{comp}-{to_tag}.mender")
            ref_name = f"librescoot-unu-{comp}-{to_tag}.delta"
            ref = os.path.join(work, ref_name)
            try:
                await self._download(f"{base}/{from_tag}/{os.path.basename(base_img)}", base_img,
                                     key, "installed image")
                await self._download(f"{base}/{to_tag}/{os.path.basename(target_img)}", target_img,
                                     key, "target image")
                await self._download(f"{base}/{to_tag}/{ref_name}", ref, key, "target delta")
                self._chain_phase[key] = "encoding fresh delta"
                meta = await self._hass.async_add_executor_job(
                    chain_mod.fresh_delta, base_img, target_img, ref, from_tag, out_path, work
                )
            finally:
                # The images are never cached: 330 MB per pair, and each pair is used once.
                await self._hass.async_add_executor_job(shutil.rmtree, images, True)
            how = f"fresh delta over {len(steps)} steps"

        self._chain_phase[key] = "hashing"
        digest = await self._hass.async_add_executor_job(chain_mod.sha256_file, out_path)
        results = chain_mod.result_hashes(meta)
        if len(results) != 1:
            raise chain_mod.ChainError(f"expected one patched member, got {sorted(results)}")
        facts = {
            "path": out_path,
            "name": out_name,
            "size": os.path.getsize(out_path),
            "sha256": digest,
            "result": next(iter(results.values())),
            "steps": len(steps),
            "from": from_tag,
            "to": to_tag,
        }
        _LOGGER.info(
            "delta chain %s %s->%s: %s into %s (%d B, result %s)",
            comp, from_tag, to_tag, how, out_name, facts["size"], facts["result"][:16],
        )
        await self._hass.async_add_executor_job(_prune_chain_dir, work)
        return facts

    async def _chain_steps(
        self, info: dict, comp: str, from_tag: str, to_tag: str
    ) -> tuple[list[str], list[int]]:
        """Tags strictly after from_tag up to and including to_tag, in the same channel, plus
        the size of each step's delta asset (from the listing, so nothing is downloaded)."""
        prefix = to_tag.split("-", 1)[0]
        if from_tag.split("-", 1)[0] != prefix:
            raise chain_mod.ChainError("start and target are in different channels")
        if from_tag >= to_tag:
            raise chain_mod.ChainError(f"{to_tag} is not newer than {from_tag}")
        url = f"https://api.github.com/repos/{info['repo']}/releases?per_page=100"
        headers = {"Accept": "application/vnd.github+json"}
        if info.get("token"):
            headers["Authorization"] = f"Bearer {info['token']}"
        session = async_get_clientsession(self._hass)
        async with session.get(url, headers=headers, timeout=ClientTimeout(total=60)) as up:
            if up.status != 200:
                raise chain_mod.ChainError(f"release listing: HTTP {up.status}")
            releases = await up.json()
        by_tag = {
            r["tag_name"]: r for r in releases
            if isinstance(r.get("tag_name"), str) and r["tag_name"].split("-", 1)[0] == prefix
        }
        tags = sorted(by_tag)
        if from_tag not in by_tag:
            raise chain_mod.ChainError(f"{from_tag} is outside the fetched release window")
        if to_tag not in by_tag:
            raise chain_mod.ChainError(f"{to_tag} is not a published release")
        steps = [t for t in tags if from_tag < t <= to_tag]
        sizes = []
        for tag in steps:
            name = f"librescoot-unu-{comp}-{tag}.delta"
            size = next(
                (a.get("size") for a in by_tag[tag].get("assets", []) if a.get("name") == name),
                None,
            )
            if not isinstance(size, int):
                raise chain_mod.ChainError(f"{tag} has no {comp} delta asset — the chain is broken there")
            sizes.append(size)
        return steps, sizes

    async def _handle_chain_bytes(self, request: web.Request) -> web.StreamResponse:
        """Serve a merged bundle with Range support, the same way the proxy serves GitHub bytes."""
        info = self._secrets.get(request.match_info["secret"])
        if info is None:
            raise web.HTTPForbidden(text="bad token")
        comp = request.match_info["component"]
        from_tag = request.match_info["from_tag"]
        to_tag = request.match_info["to_tag"]
        filename = request.match_info["filename"]
        if comp not in ("mdb", "dbc") or not _TAG_RE.match(from_tag) or not _TAG_RE.match(to_tag):
            raise web.HTTPBadRequest
        facts = self._chains.get(f"{comp}/{from_tag}/{to_tag}")
        if facts is None or not os.path.isfile(facts["path"]):
            raise web.HTTPNotFound(text="chain not built")
        if filename != facts["name"]:
            raise web.HTTPNotFound(text="wrong bundle name")
        return web.FileResponse(facts["path"], headers={"Accept-Ranges": "bytes"})

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        info = self._secrets.get(request.match_info["secret"])
        if info is None:
            raise web.HTTPForbidden(text="bad token")
        tag = request.match_info["tag"]
        filename = request.match_info["filename"]
        if ".." in tag or ".." in filename or "/" in tag or "/" in filename:
            raise web.HTTPBadRequest
        gh = f"https://github.com/{info['repo']}/releases/download/{tag}/{filename}"

        headers = {}
        if (rng := request.headers.get("Range")):
            headers["Range"] = rng
        # Deliberately NO Authorization header here. Release assets of a public repo need no token,
        # and this URL 302-redirects to the release CDN (objects.githubusercontent.com) — sending a
        # PAT would risk handing it to another host on redirect. The token is only for the GitHub
        # *API* rate limit, which this byte proxy never touches.

        # The ESP drains this stream at BLE speed (a few kB/s), so a large image takes hours and the
        # upstream connection idles under backpressure: no total timeout, only an inactivity guard.
        session = async_get_clientsession(self._hass)
        timeout = ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=120)
        resp: web.StreamResponse | None = None
        sent = 0
        try:
            async with session.get(gh, headers=headers, allow_redirects=True, timeout=timeout) as up:
                resp = web.StreamResponse(status=up.status)
                for h in _PASS:
                    if h in up.headers:
                        resp.headers[h] = up.headers[h]
                resp.headers.setdefault("Accept-Ranges", "bytes")
                await resp.prepare(request)
                if request.method != "HEAD":
                    async for chunk in up.content.iter_chunked(65536):
                        await resp.write(chunk)
                        sent += len(chunk)
                await resp.write_eof()
                return resp
        except web.HTTPException:
            raise
        except Exception as err:  # noqa: BLE001 - upstream/network failures
            if resp is not None and resp.prepared:
                # The body is already on the wire. Raising an HTTPException from here makes aiohttp
                # prepare the error response on the SAME payload writer, i.e. it appends a complete
                # "HTTP/1.1 502 Bad Gateway ..." message to the firmware bytes and keeps the
                # connection open. The ESP forwards those bytes to the scooter as payload (correct
                # offsets, wrong content) and the bundle fails its SHA-256 at COMPLETE. Cut the
                # connection instead: the ESP sees a short read and resumes from a clean offset.
                _LOGGER.warning("OTA proxy stream for %s cut after %d B: %s", gh, sent, err)
                if request.transport is not None:
                    request.transport.abort()
                return resp
            _LOGGER.warning("OTA proxy error for %s: %s", gh, err)
            raise web.HTTPBadGateway


def _delta_base_from_head(head: bytes) -> str:
    """Parse "old_artifact_name" out of the metadata.json in a truncated .delta archive.

    The archive is deliberately incomplete (only its head was fetched), so tarfile is fed member by
    member and the inevitable read error past the end is expected, not a failure.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(head), mode="r|gz") as tar:
            for member in tar:
                if not member.name.endswith("metadata.json"):
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    return ""
                meta = json.loads(fh.read().decode("utf-8"))
                name = str(meta.get("old_artifact_name", ""))
                return name[8:] if name.startswith("release-") else name
    except Exception as err:  # noqa: BLE001 - a truncated or unexpected archive just means "unknown"
        _LOGGER.debug("delta base: could not parse archive head: %s", err)
    return ""


def _ensure_dir(path: str) -> None:
    """os.makedirs' second positional argument is `mode`, not `exist_ok` - keep them apart."""
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass


def _chain_body(facts: dict) -> str:
    """One `key value` per line — cheap for the ESP to parse off a plain-HTTP GET."""
    return (
        f"name {facts['name']}\n"
        f"size {facts['size']}\n"
        f"sha256 {facts['sha256']}\n"
        f"result {facts['result']}\n"
        f"steps {facts['steps']}\n"
    )


def _prune_chain_dir(work: str) -> None:
    """Keep the cache bounded: the newest few merged bundles and their inputs."""
    merged = os.path.join(work, "merged")
    for folder, keep in ((merged, _CHAIN_KEEP), (work, _CHAIN_KEEP * 3)):
        try:
            entries = [
                (os.path.getmtime(os.path.join(folder, n)), os.path.join(folder, n))
                for n in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, n))
            ]
        except OSError:
            continue
        for _, path in sorted(entries, reverse=True)[keep:]:
            try:
                os.remove(path)
            except OSError:
                pass
