# MF800H experimental flasher

Standalone backup-first assistant for the confirmed **MF800H / MIFID,
MSM8916, 512 MB RAM, 4 GB eMMC** reference board. Not the UZ801 installer.
Start with [the complete flashing/recovery guide](docs/FLASHING.md).

**Flashing erases Android userdata and can brick the device.** Keep a verified,
private stock backup of the exact target and physical EDL recovery access.
The assistant has offline/simulated-device coverage; this package has not been
live-flashed. Charging remains disabled/unverified. Other revisions are untested.

## Host setup

Python 3.10+ is required. macOS/Linux prepare images; Windows can validate/flash
already prepared images with working libusb/WinUSB.

```sh
# macOS:
brew install python libusb dtc e2fsprogs android-platform-tools
# Debian/Ubuntu alternative:
# sudo apt-get install python3 python3-venv libusb-1.0-0 device-tree-compiler e2fsprogs adb
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Windows use `py -3 -m venv .venv`, `.venv\Scripts\python.exe -m pip install
-r requirements.txt`, and `run.bat`. Bind only the verified Qualcomm `05c6:9008`
interface to WinUSB if required; never replace a hub/keyboard driver.

Provision bkerler EDL separately from <https://github.com/bkerler/edl>, including
submodules, into `tools/edl/`. The development checkout was commit
`1cda1a616748d608667b2d13b73a28d3197ceb45`; review upstream dependencies and
licenses. Supply the **known-compatible** MSM8916 eMMC Firehose programmer as
`tools/loaders/prog_emmc_firehose_8916.mbn`, or pass `--edl-script /path/to/edl.py
--loader /path/to/programmer.mbn` to commands that contact EDL. Neither EDL nor
the programmer is distributed here. Do not select a random loader by SoC name.

```sh
sh run.sh --help
sh run.sh doctor  # dependency checks + USB enumeration; no programmer upload
python3 -m unittest discover -s flasher/tests -v
python3 -O -m unittest discover -s flasher/tests -v
```

`plan`, `prepare`, `flash` without `--execute`, and `restore` without `--execute`
are offline. Actual writes require explicit execution and exact target
confirmation, independently read back writes, and never reboot automatically.
No force/skip-backup/resume bypass exists.

`profiles/` contains only the reference DTS and necessary battery LUT metadata.
`cellular/` is the independent modem-discovery recovery timer and required LTE
setup; read [its instructions](cellular/README.md) after first boot. LCD and WebUI
are separate releases. No firmware ZIP, stock dump, calibration, prepared image
or historical per-device installation record is included.

See [RELEASE.md](RELEASE.md) for publication/license gates.
