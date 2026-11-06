# MF800H flashing assistant

Start with **[the complete flashing and recovery guide](../docs/FLASHING.md)**.
Run from the workspace root:

```sh
sh run.sh --help
python3 -m unittest discover -s flasher/tests -v
```

| Command | Effect |
| --- | --- |
| `doctor` | Checks host dependencies; USB enumeration only, no programmer upload |
| `backup --execute` | Uploads a RAM programmer, reads stock eMMC, checks both GPTs and independently rereads boot + all seven factory partitions |
| `prepare` | Offline, checksum-pinned firmware expansion and per-device image generation |
| `plan` | Offline backup, calibration, image, GPT and write-boundary validation |
| `flash` | Offline plan unless `--execute` and exact target confirmation are supplied |
| `restore` | Offline backup validation unless `--execute` and exact confirmation are supplied |
| `reboot` | Separate explicit reset, only after rechecking a successful run on the device |

There is no unattended download/install, auto-reboot, skip-backup, identity
bypass, force, or automatic write-resume flag. Existing images, backups and run
directories are never overwritten. A started image set gets `.flash-started.json`;
the old `flash-state.json` is also honored. State/logs go in a **new** run directory.
Only `reboot` updates that run's state to record the explicit reset request.

## Implementation

- `images.py`: streaming SHA-256, dual-GPT CRC/bounds checks, sidecar verification,
  deterministic TZ-only GPT patch and restricted ordered write plan.
- `prepare.py`: Android sparse v1 expansion and verified `debugfs` writes; uses
  `../profiles/{upstream-mf800.dts,mf800h.dts,stock_3_properties.json}` as the
  reference board profile. No private backup is included as a template.
- `device.py`: lazy PyUSB import, exactly-one-device checks, exclusive workspace
  lock, subprocess deadlines, independent read-backs and private per-command logs.
- `cli.py`: explicit workflows, confirmation phrases and persistent failure state.

Runtime uses Python 3.10+ and separately provisioned bkerler EDL dependencies.
Offline validation and tests use only Python's standard library; preparation
also needs `dtc` and e2fsprogs. Do not treat downloaded manifests or Python/DTS
sources as trusted merely because their self-declared hashes match.

## Validation scope

The original hardware procedure was verified on one MF800H. The **new tool** has
offline real-image preparation/plan checks and simulated-device regression tests;
it has not been live-flashed as part of its development. Same-label hardware
variants still require manual board/wiring/battery confirmation. USB `05c6:9008`
does not identify a model. Factory partition fingerprints are a wrong-target
defense, not cryptographic hardware attestation. Stop if any identity check fails.
