# Release candidate checklist

- [ ] Owner chooses a release version/tag and project license. No license for
  these components was present in the source workspace; packaging does not
  grant a new license or borrow the unrelated installer's license.
- [ ] Review third-party rights/notices before public redistribution: Go/npm
  dependencies for the WebUI; upstream device-tree sources and stock-derived
  battery/panel data for hardware packages. Preserve upstream licenses.
- [ ] Review the README, hardware restrictions and safety/recovery instructions.
- [ ] Validate on the intended board/OS. Host tests do not establish hardware,
  charging, native ARM64 or carrier compatibility.
- [ ] Verify `SHA256SUMS` before archiving/signing/publishing. Rebuild checksums
  if any distributed files change; do not package local state or dependencies.

MF800H support is experimental and reference-board-specific. Linux charging is
disabled/unverified; displayed capacity is uncalibrated. Flashing requires the
target's own verified stock backup. UZ801 LED wiring is not MF800H wiring.

No credentials, TLS keys, calibration or per-unit images are needed in a public
release. Firmware downloads and bkerler EDL/Firehose are not bundled here.
