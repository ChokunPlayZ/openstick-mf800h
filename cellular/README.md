# MF800H cellular setup and modem recovery

**Required for reliable LTE on this experimental MF800H port.** This component
is independent of the WebUI and LCD daemon, and is carrier-neutral.

## Why this is required

On 2026-11-01, this unit's factory modem firmware (`HM02_MODEM_V1.2`) reset shortly
after boot (`sys_m.c:603: THIS IS INTENTIONAL RESET`). The kernel brought the
modem and `/dev/wwan0qmi0` back, but ModemManager 1.20.4 failed to recreate it:
`Unsupported device: at least a QMI port is required`. Consequently `mmcli -L`
reported no modems, even though the firmware was running.

A ModemManager restart recovered SIM detection and LTE registration. There was
a separate configuration requirement: the original experimental image saved
`lte` with **autoconnect disabled**. Registration alone does not provide an
internet data connection. Both the recovery timer and LTE autoconnect settings
below are needed; the timer is a workaround, not a firmware/kernel fix.

Do not edit or rerun historical flashing scripts or overwrite installation
manifests/backups to apply this runtime fix.

## 1. Enable persistent, provider-auto-configured data

Run on the stick over SSH. These commands assume the existing profile is named
`lte`; inspect `nmcli connection show` first and use the correct name/UUID on
other installations. Back up its root-owned NetworkManager keyfile first.

```sh
# Provider database for NetworkManager's SIM-based APN lookup.
sudo apt-get install mobile-broadband-provider-info

# For an ordinary public-carrier SIM: leave carrier/APN selection automatic.
# Do not apply these APN-clearing settings to an existing custom/private APN.
sudo nmcli connection modify lte gsm.auto-config yes gsm.apn '' gsm.username '' gsm.password '' gsm.network-id '' gsm.sim-id '' gsm.sim-operator-id ''

# Test activation before enabling it at boot.
sudo nmcli --wait 90 connection up lte
sudo nmcli connection modify lte connection.autoconnect yes connection.autoconnect-retries 0
```

`connection.autoconnect-retries=0` means unlimited retries, **not** zero attempts.
This allows reconnection after a modem reset, a temporary outage or a delayed
network registration. NetworkManager manages the interface, route and DNS;
do not use `mmcli --simple-connect` as a substitute for the managed profile.

### Other carriers, MVNOs, travel SIMs and private APNs

- There is **no hard-coded AIS APN or operator code** in the recovery component.
  With `gsm.auto-config=yes`, NetworkManager chooses provider settings from the
  installed `mobile-broadband-provider-info` database using the current SIM.
  A generic profile must not be pinned to the previous SIM/operator.
- Automatic lookup cannot guarantee every ISP: an unknown/MVNO/private APN,
  subscription restriction, SIM PIN, unsupported LTE band, lack of coverage or
  carrier lock must be addressed separately. Only AIS was live-tested here.
- For an ISP that requires a specific APN, use its published settings instead:

  ```sh
  sudo nmcli connection modify lte gsm.auto-config no gsm.apn 'YOUR_ISP_APN'
  # Set gsm.username/gsm.password only if your ISP requires them.
  # Set ipv4/ipv6 methods according to the provider if dual-stack is unsupported.
  sudo nmcli --wait 90 connection up lte
  ```

- Preserve intentional roaming restrictions; some travel SIMs need roaming,
  which can incur charges. The recovery service does not alter roaming policy.
- Power off before physically changing the SIM. On next boot the same generic
  profile can select the new carrier; switch back to auto-config if leaving a
  manually configured APN. No SIM PIN/PIN2/PUK is guessed or changed.

## 2. Install the modem-discovery recovery timer

### Standalone installation (on the stick, from a copy of this folder)

```sh
sudo install -d -m 755 /usr/local/lib/openstick-cellular /usr/local/share/openstick-cellular
sudo install -m 644 cellular_recovery.py /usr/local/lib/openstick-cellular/
sudo install -m 644 README.md /usr/local/share/openstick-cellular/
sudo install -m 644 openstick-cellular-recovery.service openstick-cellular-recovery.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openstick-cellular-recovery.timer
```

### Recovery scope and safeguards

- Checks every approximately 30 seconds, starting 45 seconds after boot.
- Only runs on MF800H hardware with `/dev/wwan0qmi0` present, an already-running
  ModemManager and WWAN enabled. It respects a stopped service or disabled WWAN.
- Restarts **only ModemManager**, and only when it exports no modems for at
  least another 30 seconds. Existing modems (including SIM-missing/PIN-locked,
  disabled, unregistered or connected ones) are left alone.
- At most one restart attempt per five minutes; failures are rate-limited too.
  State lives in `/run`, survives separate timer invocations and resets at boot.
  Invalid diagnostic output/state causes an error, not a speculative restart.
- Never reboots, flashes, resets modem firmware, changes calibration, sends AT
  commands, sets an APN/operator/band/PIN, or touches Wi-Fi/USB settings.
- NetworkManager reconnects only eligible profiles with autoconnect enabled.
  This does not override an explicit manual disconnect or radio disable.

## Verify and troubleshoot

```sh
systemctl is-enabled openstick-cellular-recovery.timer
systemctl list-timers openstick-cellular-recovery.timer
sudo journalctl -u openstick-cellular-recovery.service -b --no-pager
mmcli -L
mmcli -m any
nmcli -f connection.autoconnect,connection.autoconnect-retries,gsm.auto-config,gsm.apn connection show lte
nmcli device status
ip -4 route
ping -I wwan0 -c 3 1.1.1.1
curl --interface wwan0 --connect-timeout 10 --max-time 20 -I https://example.com
```

An inactive **service** between checks is normal; the **timer** must be active.
Read `journalctl -u ModemManager -b` and `journalctl -k -b` if discovery fails.
No QMI port means a firmware/kernel problem outside this timer's recovery scope.

Local regression tests (also included in `webui/make test`):

```sh
python3 -m unittest discover -s cellular -p 'test_*.py' -v
```

To disable only automatic discovery recovery:

```sh
sudo systemctl disable --now openstick-cellular-recovery.timer
```

To disable automatic LTE connection separately:

```sh
sudo nmcli connection modify lte connection.autoconnect no
sudo nmcli connection down lte
```

Only AIS was historically live-tested. No new hardware test is implied by this package.
