# Flashing an MF800H: stock Android → experimental Debian/OpenStick

This is the end-to-end procedure for the **MF800H / Android model `MIFID`,
MSM8916, 512 MB RAM, 4 GB eMMC** reference board. It is not an upstream-supported
MF800H release and is **not** the UZ801 procedure. The original installation was
verified on one unit on 2026-11-01. Other revisions have not been validated.

**Flashing replaces the boot chain and erases Android userdata. A failed write
can make the device unbootable.** Proceed only with physical recovery access,
stable power, a verified backup of **this exact unit**, and acceptance of that risk.
The new assistant was tested offline and against simulated devices, not by
reflashing the working unit. Commands below are instructions for the operator;
reading this guide or running `plan` never flashes hardware.

## Contents

1. [Scope and prerequisites](#1-scope-and-prerequisites)
2. [Prepare the host](#2-prepare-the-host)
3. [Identify stock hardware and enter EDL](#3-identify-stock-hardware-and-enter-edl)
4. [Create a private stock backup](#4-create-a-private-stock-backup)
5. [Prepare per-device Linux images offline](#5-prepare-per-device-linux-images-offline)
6. [Review, flash and explicitly reboot](#6-review-flash-and-explicitly-reboot)
7. [First boot and security](#7-first-boot-and-security)
8. [LTE, LCD and WebUI completion](#8-lte-lcd-and-webui-completion)
9. [Troubleshooting and stock recovery](#9-troubleshooting-and-stock-recovery)
10. [Technical reference and verification](#10-technical-reference-and-verification)

## 1. Scope and prerequisites

Confirm the case label **and** board/SoC/RAM details; a similar case or `9008`
USB ID is insufficient. The reference battery label is **3.8 V nominal,
2100 mAh, 7.98 Wh, 4.35 V maximum**. Do not apply this battery/LCD profile to an
unknown revision. Linux charging remains disabled, charging itself is unverified,
and percentage/80%-limit claims would be incorrect. Use a healthy charged battery
and reliable USB supply; do not rely on this port to recharge during flashing.

You need:

- macOS or Linux for image preparation; Python **3.10+**, `dtc`, e2fsprogs,
  libusb, and bkerler EDL. Windows can flash/validate **already prepared** images
  with Python + working libusb; native Windows preparation is not supported.
- A data-capable USB cable, a direct/stable USB port and no other EDL devices.
  Disable host sleep. Never swap devices during a run or run another EDL tool.
- At least **12 GiB free** for backup, preparation, logs and scratch read-backs,
  plus a separate offline/encrypted copy of the stock backup.
- Stock ADB access if available; working access to EDL for recovery. Do not
  proceed if you cannot reliably re-enter EDL independently of Linux boot.
- This standalone package and a separately obtained, reviewed bkerler EDL checkout
  plus the known-compatible MSM8916 eMMC programmer. See [host setup](../README.md).

**Never use** `openstick-installer/calibration/`, its UZ801 backups,
`openstick-uz801-linux.zip`, a generic `full_emmc.bin`, or the release's
`gpt_both0.bin` on an MF800H. Do not flash the release ZIP directly or run its
generic fastboot scripts. Every unit needs its own backup, GUIDs and manifest.

## 2. Prepare the host

All host examples start in this package's root. Complete the
[standalone host setup](../README.md#host-setup) first. The wrapper uses this
package's `.venv`, never the UZ801 installer. EDL and the programmer are not bundled.

```sh
sh run.sh doctor
sh run.sh --help
mkdir -p backups runs downloads
```

`doctor` enumerates USB but does not upload a programmer. Zero EDL devices is
normal before step 3. `plan`, `prepare`, `flash` without `--execute`, and
`restore` without `--execute` do not access USB at all.

## 3. Identify stock hardware and enter EDL

On the host, while stock Android is running:

```sh
adb devices -l
adb -s YOUR_ADB_SERIAL shell getprop ro.product.model
adb -s YOUR_ADB_SERIAL shell getprop ro.board.platform
adb -s YOUR_ADB_SERIAL shell cat /proc/meminfo
adb -s YOUR_ADB_SERIAL shell cat /proc/partitions
```

Record the serial, model, partition layout and physical battery/board details
privately. The reference unit reported `MIFID` and MSM8916; installed memory
reported by the OS is less than the physical 512 MB. Stop on unexpected hardware.
Always record your own unit's ADB serial; no default target is supplied.
ADB and Qualcomm EDL serial numbers differ.

If the stock firmware permits it:

```sh
adb -s YOUR_ADB_SERIAL reboot edl
sh run.sh doctor
```

Expect exactly one Qualcomm **`05c6:9008`** device. ADB disappearing is normal.
`fastboot` mode, `900e`, and a generic serial port are not equivalent to EDL.
If ADB reboot is denied, use only a **board-revision-verified** EDL entry method.
No test-point location or button sequence has been verified for all MF800H
revisions in this workspace. Do not short guessed pads, power rails or battery
terminals. Obtain the correct board documentation before opening/shorting hardware.

## 4. Create a private stock backup

Choose a **new**, descriptive path; replace `YOUR_ADB_SERIAL` below everywhere.
The board-confirmation argument is your acknowledgement, not model autodetection.

```sh
sh run.sh backup \
  --adb-serial YOUR_ADB_SERIAL \
  --confirm-board MF800H-MSM8916-512M \
  --output backups/my-mf800h-stock \
  --execute
```

This uploads the programmer into RAM but does **not** write eMMC or reboot.
Allow several minutes. It creates:

```text
my-mf800h-stock/
  full_emmc.bin           complete 3,909,091,328-byte stock user-area image
  device_info.json        completion, size, identity label and SHA-256
  gpt.json                parsed, dual-CRC-checked partition information
  calibration/           fsc, fsg, modem, modemst1, modemst2, persist, sec
  NNN-*.log              independent read/verification logs
```

Both GPT copies must agree and pass header/table CRC checks. The tool rereads
`boot` and **all seven** factory partitions independently and compares hashes.
Sidecars must exactly match their ranges in the full dump; empty/sanitized
factory data is rejected (an all-zero `fsc` is allowed).

Copy the entire backup directory to a second drive and verify its SHA-256 there.
You can validate that copy without USB using
`sh run.sh restore --backup /path/to/second-copy` (no `--execute`).
Keep it private: it contains unique calibration, identifiers and original user
data. A checksum is integrity evidence, not encryption. No existing historical
backup or installation record is modified by these commands.

**Already running Linux?** Do not call a Linux dump a stock backup. Use the
original verified stock backup for recovery; this backup command rejects the
converted TZ layout. Never use another unit's backup.

## 5. Prepare per-device Linux images offline

Use **LongQT-sea/OpenStick-Builder v1.2**, asset `openstick-debian.zip`:

- [Release page](https://github.com/LongQT-sea/OpenStick-Builder/releases/tag/v1.2)
- [Download asset](https://github.com/LongQT-sea/OpenStick-Builder/releases/download/v1.2/openstick-debian.zip)
- Size: **150,137,574 bytes**
- SHA-256: **`2dbbb357ea9e426ce610ba1eaf76869809331ac58ad4072e6493f62dd85a63be`**

The local reference copy is `downloads/longqt-debian-v1.2.zip`. If absent,
download the exact asset to that path. The assistant verifies its pinned hash
before extracting anything. Do not substitute a newer release: the kernel, DTB,
boot-chain images and filesystem modifications are version-specific.

```sh
sh run.sh prepare \
  --backup backups/my-mf800h-stock \
  --firmware downloads/longqt-debian-v1.2.zip \
  --output runs/my-mf800h-images \
  --confirm-board MF800H-MSM8916-512M
```

Preparation is offline and never modifies the backup, original archive or
historical scripts. It expands Android sparse images, builds the conservative
headless DTB, makes a 32 MiB boot filesystem, embeds **your** root/boot PARTUUIDs,
sets NCM + ACM USB, changes the hostname/hotspot, and disables LTE autoconnect
for first boot. `debugfs` writes are reread, and both filesystems pass `e2fsck -fn`.

Reference hardware sources are
`profiles/{upstream-mf800.dts,mf800h.dts,stock_3_properties.json}`.
The stock OCV curve is retained as **unvalidated** metadata. This is a reference
board profile, not a tool that reverse-engineers unknown hardware automatically.
The initial image does not include the later LCD daemon, WebUI or LTE recovery timer.

Output includes `manifest.json`, padded boot firmware, rootfs, the boot image,
four GPT segments and preparation sources. Keep the whole output. A failed
preparation directory is not resumable; inspect its error and use a new name.

## 6. Review, flash and explicitly reboot

First run a **fully offline** check:

```sh
sh run.sh plan \
  --backup backups/my-mf800h-stock \
  --images runs/my-mf800h-images
```

Review the serial, stock hash and every write region. Confirm the offline backup
copy exists, power is stable, and only this unit is connected in EDL. Then:

```sh
sh run.sh flash \
  --backup backups/my-mf800h-stock \
  --images runs/my-mf800h-images \
  --run-dir runs/my-mf800h-flash-01 \
  --execute
```

Type the printed **`FLASH <serial> <first-12-stock-hash>`** phrase exactly.
For scripted operation, pass that exact string via `--confirm 'FLASH ...'`;
there is no generic `--yes` bypass. Without `--execute`, `flash` only prints the
offline plan and does not even create the run directory.

Before the first write, live primary/backup GPT, the TZ gap and all seven factory
partitions must match the backup. The assistant checks that exactly one EDL
device is present before each command and rereads the FSG/SEC/PERSIST fingerprint
before each write. Every written region is read back and SHA-256 checked before
continuing; the seven factory partitions are checked again at the end. A mismatch,
timeout or interruption stops the run with **no automatic reboot or retry**.

On success, inspect `my-mf800h-flash-01/state.json`: `completed` must be `true`,
all eleven regions verified, and `factory_partitions_unchanged` must list seven
entries. Only then explicitly reboot:

```sh
sh run.sh reboot \
  --state runs/my-mf800h-flash-01/state.json \
  --run-dir runs/my-mf800h-reboot-01 \
  --execute
```

Confirm **`REBOOT <serial> <hash-prefix>`**. The tool verifies the device and
written regions again before reset; this extra read-back takes time. A completed
host-side state alone is not trusted. Do not change devices between steps.
If reset times out, inspect USB and the serial console: `reboot_issued` records
an **attempt**, not proof of boot. Do not clear that flag and blindly send resets.

The tool records `.flash-started.json` in the prepared image set before writing
and refuses to flash it again. It also refuses image sets with historical
`flash-state.json`. Do not delete these markers to force a retry.

## 7. First boot and security

Allow several minutes for initial rootfs resize and SSH host-key generation.
A blank/stale LCD is **expected** for the initial headless image and does not
prove boot failure. Look for USB NCM networking and the ACM serial console.

| Initial access | Value |
| --- | --- |
| USB management / LAN bridge `br0` | `192.168.100.1` |
| SSH | `ssh user@192.168.100.1` |
| Default Debian password | `1` |
| Hotspot SSID / password | `OpenStick-MF800H` / `openstick` |
| USB ACM console | 115200 baud; local root autologin |
| Kernel | `6.12.1-msm8916`, ARM64 |

Keep initial setup isolated. Verify the SSH host-key fingerprint through the
local USB console before accepting it. A reflash legitimately generates a new
key; investigate rather than globally disabling host-key checking. If DHCP
fails, inspect the USB interface and choose a temporary unused address such as
`192.168.100.2/24` on that interface, **without a default gateway**. Remove the
temporary configuration after resolving DHCP. Avoid a conflicting host LAN/VPN
using `192.168.100.0/24`.

Run **on the device**:

```sh
uname -a
cat /proc/device-tree/model
lsblk -o NAME,SIZE,FSTYPE,PARTLABEL,PARTUUID,MOUNTPOINTS
findmnt /
findmnt /boot
df -h /
systemctl --failed
ip -br address
nmcli device status
mmcli -L
passwd
sudo nmcli connection modify hotspot wifi-sec.psk 'YOUR_NEW_STRONG_PASSWORD'
```

Changing the hotspot password does not activate the new value until the profile
is reactivated; do that over USB to avoid lockout. Set up SSH keys and review
the USB root-autologin console before lending the device or using untrusted USB
hosts. Confirm root is on the original `userdata` partition and `/boot` on the
original `boot` partition. A SIM-missing/no-modem result is not a flashing success
criterion for LTE; complete the next section.

**APT:** use explicit Debian **Bookworm** repositories, not rolling `stable`.
The initial firmware predates this workspace's Bookworm pinning. Inspect both
`/etc/apt/sources.list` and `/etc/apt/sources.list.d/` and remove/replace conflicting
suite entries deliberately before `apt-get update` or upgrades. Example official
entries (retain required firmware components for your install):

```text
deb https://deb.debian.org/debian bookworm main contrib non-free non-free-firmware
deb https://deb.debian.org/debian bookworm-updates main contrib non-free non-free-firmware
deb https://security.debian.org/debian-security bookworm-security main contrib non-free non-free-firmware
```

Do not blindly run `apt upgrade` against `stable` or attempt to downgrade a mixed
suite installation using these three lines alone. Check `apt-cache policy`,
`sudo apt-get check`, and `sudo dpkg --audit` before adding optional software.

## 8. LTE, LCD and WebUI completion

### LTE — required for persistent mobile data

Complete **both** steps in [cellular/README.md](../cellular/README.md):

1. Install `mobile-broadband-provider-info`, activate/test the existing `lte`
   profile, then enable autoconnect with unlimited retries. Use SIM/provider
   auto-configuration for ordinary carrier SIMs; enter the provider's own APN
   for private/MVNO cases. Never guess PIN/PUK or enable roaming without consent.
2. Install/enable `openstick-cellular-recovery.timer`. This works around the
   observed ModemManager rediscovery failure after the factory modem resets.

Registration/signal bars alone do not prove data. Check an LTE default route,
interface-bound ping and certificate-verified HTTPS as documented there.
Only AIS was live-tested; other providers/bands/SIM restrictions are not guaranteed.

### LCD and WebUI — separately distributed components

Use the separate **lcd** package for the confirmed GC9107 board (kernel 6.12.1),
and the **webservice** package for the console. Each has standalone build/install
instructions; neither is bundled here. Keep USB SSH as a recovery path.
The console should listen only on `192.168.100.1` / `br0`, never uplinks.
No package enables charging or adds a charge-limit feature.

Final acceptance: reboot deliberately when safe; verify a new boot ID, root/boot
mounts, USB SSH, hotspot clients, active recovery timer, LTE data if required,
and independent LCD/WebUI services. Do not declare charging validated based on
an icon, voltage increase or inherited sysfs percentage.

## 9. Troubleshooting and stock recovery

| Symptom | Safe next action |
| --- | --- |
| No `9008`, permission/backend error | Check data cable, exact USB ID, libusb/udev/WinUSB, and competing host tools. Do not write until detection is reliable. |
| Sahara/authentication/loader failure | Stop and confirm board + known-good programmer. Do not force writes or try unrelated loaders. |
| Multiple EDL devices | Disconnect the others; never rely on discovery order. |
| Backup/manifest/calibration mismatch | Stop. Check the unit, source paths and hashes. Never edit a manifest hash to silence a failure. |
| Interrupted/failed write or read-back | Do not reboot or unplug just to retry. Keep EDL available, preserve state/logs and restore the verified stock backup after diagnosis. |
| Blank LCD but USB SSH works | Expected with headless firmware; add the correct LCD DTB/firmware and daemon separately. |
| USB exists but SSH unreachable | Inspect host address/route, overlapping VPN/subnet, NCM support and the ACM console; do not assume a boot failure. |
| No modem or no LTE data | Follow cellular recovery + APN/autoconnect checks. Do not replace factory modem/NV partitions. |
| Old flash marker / existing run directory | Inspect its record. Use a new run path only after diagnosis; never delete markers to repeat a partial flash. |

### Full verified stock restore

Enter EDL for **the same unit**. A restore erases Linux and all later data and
restores the backed-up factory radio state. Validate offline first:

```sh
sh run.sh restore --backup backups/my-mf800h-stock
sh run.sh restore \
  --backup backups/my-mf800h-stock \
  --run-dir runs/my-mf800h-restore-01 \
  --execute
```

Confirm **`RESTORE <serial> <hash-prefix>`**. The tool checks live FSG/SEC/PERSIST
against the backup, writes the full stock image, then rereads **the whole image**
and verifies its SHA-256. GPT damage does not by itself prevent this raw restore.
Successful completion still leaves the device in EDL. Explicitly reboot with:

```sh
sh run.sh reboot \
  --state runs/my-mf800h-restore-01/state.json \
  --run-dir runs/my-mf800h-stock-reboot-01 \
  --execute
```

If factory identity partitions legitimately changed or were damaged, automated
restore **refuses**. There is no force/identity-bypass switch. Preserve new reads,
verify hardware identity and seek reviewed, board-specific manual recovery.
FSG/SEC/PERSIST are a conservative factory-data fingerprint, not authenticated
chip identity. A manually supplied ADB serial is a label, not proof of ownership
or the attached device's identity.

The workspace `.edl.lock` prevents two assistant processes. If a crash leaves
it behind, read its PID and confirm **no EDL process is still running** before
removing that one lock file. This lock cannot stop unrelated third-party tools.
Do not remove `.flash-started.json`, old `flash-state.json`, backup files or logs.
No automatic partial-write resume is implemented: boot-chain/GPT interruption
needs diagnosis, not speculative skip/retry logic.

## 10. Technical reference and verification

The accepted write order is:

1. `userdata` (Linux rootfs), then `boot` (32 MiB ext2).
2. Relocated `tz`, then `rpm`, `hyp`, `aboot`.
3. Backup GPT entries/header, then primary entries/header.
4. **`sbl1` last**.

The release's 64-bit TZ exceeds the original 512 KiB slot. Only TZ's GPT entry
is moved to a verified-empty **1 MiB at LBA 65536 (byte 33,554,432)**. Both GPT
CRCs are recomputed. All GUIDs, attributes and unrelated partition locations
are retained. The original backup slots remain, but are not a guarantee of
automatic fallback. The seven factory partitions are excluded from installation
writes. `plan` reconstructs the expected GPT patch rather than trusting arbitrary
GPT blobs named in a manifest.

Full-image means the eMMC **user area** in this procedure, not RPMB or separate
hardware boot areas. Do not change eMMC provisioning, bootable-storage selection,
secure-boot settings or fuse state. No such operations are required here.

Tests, with no device required:

```sh
python3 -m unittest discover -s flasher/tests -v
python3 -O -m unittest discover -s flasher/tests -v
```

Safety checks use explicit exceptions, not assertions disabled by `python -O`.
Historical development also included an offline rebuild from the pinned archive
and validation of the original per-unit manifest. Private installation records
are deliberately excluded. Packaging does not imply a new live-flash test.
