// USB chip detection for the Librescoot-BLE-Client setup wizard.
//
// Runs in the browser: Web Serial + esptool-js talk to the ESP's ROM bootloader, read chip type,
// revision, embedded flash/PSRAM, flash size and MAC, then reset the ESP. Read only — esptool-js
// loads its small helper into the ESP's RAM to read the flash ID, nothing is written to flash.
// The result goes to Home Assistant over the websocket command librescoot_ble_client/usb_detected;
// the wizard's chip step then offers it.

import { ESPLoader, Transport } from "./esptool-js-0.7.0.js";

const css = `
  :host { display: block; padding: 16px; max-width: 760px; margin: 0 auto;
          color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, sans-serif); }
  h1 { font-size: 22px; font-weight: 400; margin: 8px 0 16px; }
  .card { background: var(--card-background-color, #fff); border-radius: 12px; padding: 16px;
          box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.2)); margin-bottom: 16px; }
  button { font: inherit; padding: 8px 16px; border-radius: 8px; border: none; cursor: pointer;
           background: var(--primary-color); color: var(--text-primary-color, #fff); margin: 4px 8px 4px 0; }
  button.secondary { background: transparent; color: var(--primary-color); border: 1px solid var(--primary-color); }
  button:disabled { opacity: .5; cursor: default; }
  .warn { color: var(--warning-color, #b26a00); }
  .err { color: var(--error-color, #c00); }
  .ok { color: var(--success-color, #2e7d32); }
  pre { background: var(--secondary-background-color); padding: 8px; border-radius: 8px;
        max-height: 220px; overflow: auto; font-size: 12px; white-space: pre-wrap; }
  ul { padding-left: 20px; } li { margin: 4px 0; }
  table { border-collapse: collapse; } td { padding: 2px 12px 2px 0; vertical-align: top; }
`;

class LibrescootUsbPanel extends HTMLElement {
  constructor() {
    super();
    this._log = [];
    this._busy = false;
    this._raw = null;
    this._result = null;
    this._error = null;
    this.attachShadow({ mode: "open" });
  }

  set hass(hass) { this._hass = hass; if (!this._rendered) this._render(); }
  set narrow(_v) {}
  set panel(_v) {}

  _supported() {
    if (!window.isSecureContext) return "insecure";
    if (!("serial" in navigator)) return "noserial";
    return null;
  }

  _render() {
    this._rendered = true;
    const blocked = this._supported();
    const r = this._result;
    let body = "";
    if (blocked === "insecure") {
      body = `<p class="err">The browser allows USB access only on a secure page. Home Assistant is open
        over plain <code>http://</code> here — open it over HTTPS (your own certificate or Home Assistant Cloud),
        or choose the board by hand in the setup dialog.</p>`;
    } else if (blocked === "noserial") {
      body = `<p class="err">This browser has no Web Serial. Use Chrome or Edge on a desktop computer
        (not Firefox, Safari or a phone), or choose the board by hand in the setup dialog.</p>`;
    } else {
      body = `<p>Connect the ESP to <b>this computer</b> by USB, then press <b>Detect</b> and pick its port.</p>
        <ul>
          <li>Close the ESPHome Device Builder's log or any other program that has the port open.</li>
          <li>Boards with a USB-serial chip (CH340 / CH9102 "WCH", CP210x) need its driver on this computer.
              On an ESP32-S3 with two USB sockets, the one marked <i>UART</i> / <i>COM</i> is the reliable one.</li>
          <li>If it does not connect: hold <b>BOOT</b>, press <b>RESET</b> (or plug in while holding BOOT), release BOOT, try again.</li>
        </ul>
        <button id="detect" ${this._busy ? "disabled" : ""}>${this._busy ? "Reading…" : "Detect"}</button>`;
    }
    let result = "";
    if (this._error) result = `<p class="err">${this._esc(this._error)}</p>`;
    if (this._raw) {
      result += `<table>
        <tr><td>Chip</td><td>${this._esc(this._raw.description || this._raw.chip_name)}</td></tr>
        <tr><td>Features</td><td>${this._esc((this._raw.features || []).join(", ") || "—")}</td></tr>
        <tr><td>Flash</td><td>${this._esc(this._raw.flash_size || "unknown")}</td></tr>
        <tr><td>MAC</td><td>${this._esc(this._raw.mac || "—")}</td></tr></table>`;
    }
    if (r && r.ask_psram) {
      result += `<p><b>Does the module have PSRAM?</b> The chip cannot tell for external PSRAM. Look at the
        metal shield or the shop listing: <b>N16R8</b> / <b>N8R8</b> = yes (8 MB), <b>N16</b> / <b>N8</b> without R = no.</p>
        <button id="psram-yes">Yes — R8 (8 MB PSRAM)</button>
        <button id="psram-no" class="secondary">No PSRAM</button>`;
    } else if (r && r.error) {
      result += `<p class="err">${this._esc(r.error)}</p>`;
    } else if (r && r.chip) {
      result += `<p class="ok"><b>Board: ${this._esc(r.label)}</b></p>
        <p>Go back to the setup dialog and choose <b>"Use the board detected via USB"</b> in the board step
        (valid for 30 minutes). You can close this tab and unplug the ESP — the first install from the
        ESPHome Device Builder uses the same USB connection.</p>`;
    }
    this.shadowRoot.innerHTML = `<style>${css}</style>
      <h1>Librescoot-BLE-Client — detect the ESP via USB</h1>
      <div class="card">${body}</div>
      ${result ? `<div class="card">${result}</div>` : ""}
      ${this._log.length ? `<div class="card"><details><summary>Log</summary><pre>${this._esc(this._log.join("\n"))}</pre></details></div>` : ""}`;
    const b = this.shadowRoot.getElementById("detect");
    if (b) b.addEventListener("click", () => this._detect());
    const y = this.shadowRoot.getElementById("psram-yes");
    if (y) y.addEventListener("click", () => this._send(true));
    const n = this.shadowRoot.getElementById("psram-no");
    if (n) n.addEventListener("click", () => this._send(false));
  }

  _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  async _detect() {
    let port;
    try {
      port = await navigator.serial.requestPort();
    } catch (e) {
      return; // the user closed the port picker
    }
    this._busy = true; this._error = null; this._raw = null; this._result = null; this._log = [];
    this._render();
    const terminal = {
      clean: () => {},
      writeLine: (l) => this._log.push(l),
      write: (t) => this._log.push(t),
    };
    const transport = new Transport(port, false);
    try {
      const loader = new ESPLoader({ transport, baudrate: 115200, terminal });
      await loader.main();
      const chip = loader.chip;
      const raw = {
        chip_name: chip.CHIP_NAME,
        description: await chip.getChipDescription(loader),
        features: (await chip.getChipFeatures(loader)).map((f) => f.trim()),
        mac: await chip.readMac(loader),
        flash_size: (await loader.detectFlashSize()) || null,
      };
      if (chip.CHIP_NAME === "ESP32") {
        // Classic: major from esptool-js, minor = wafer_version_minor (eFuse BLK0 word 5, bits 24-25).
        raw.rev_major = await chip.getChipRevision(loader);
        raw.rev_minor = ((await chip.readEfuse(loader, 5)) >> 24) & 0x3;
      } else if (chip.getMajorChipVersion) {
        raw.rev_major = await chip.getMajorChipVersion(loader);
        raw.rev_minor = await chip.getMinorChipVersion(loader);
      }
      this._raw = raw;
      try { await loader.after("hard_reset"); } catch (e) { /* the port may already be gone */ }
    } catch (e) {
      this._error = `Could not read the ESP: ${e && e.message ? e.message : e}`;
    } finally {
      try { await transport.disconnect(); } catch (e) { /* already closed */ }
      this._busy = false;
    }
    if (this._raw) await this._send(undefined);
    else this._render();
  }

  async _send(psram) {
    const msg = { type: "librescoot_ble_client/usb_detected", ...this._raw };
    if (psram !== undefined) msg.psram = psram;
    for (const k of Object.keys(msg)) if (msg[k] === null || msg[k] === undefined) delete msg[k];
    try {
      this._result = await this._hass.callWS(msg);
    } catch (e) {
      this._error = `Home Assistant did not accept the result: ${e && e.message ? e.message : e}`;
    }
    this._render();
  }
}

customElements.define("librescoot-usb-panel", LibrescootUsbPanel);
