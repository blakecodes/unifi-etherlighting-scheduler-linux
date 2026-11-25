# UniFi Etherlighting Scheduler for Linux

Set the port LED brightness on a UniFi Etherlighting switch from a Raspberry Pi or another machine on the same network. The Python script can run once from the command line or on a daily systemd timer. It uses the Python standard library.

The included timer sets **100% at 7 AM** and **1% at 11 PM** in `America/Chicago`. If the host was offline at a scheduled time, the next run applies the level appropriate for the current time. The script preserves the other Etherlighting settings, skips writes when brightness is already correct, and checks the saved value after a write.

## Requirements

- Python 3.9 or later with IANA time zone data for `--daily-schedule`.
- A reachable UniFi OS console running UniFi Network and a switch that reports Etherlighting brightness. The script was tested on a USW Pro Max 24 PoE.
- A dedicated local console account. In UniFi, create a local-only admin with **Network Full Management** and no permissions for the other applications. See the [local user setup guide](https://www.home-assistant.io/integrations/unifi/#local-user).
- For unattended scheduling, a Linux host with systemd 250 or later and `systemd-creds`. The service uses [encrypted systemd credentials](https://www.freedesktop.org/software/systemd/man/latest/systemd-creds.html).

The script uses UniFi Network's internal device settings endpoint. If firmware changes the endpoint or its response, the script exits with an error if it cannot verify the brightness.

## Run once

Clone the repository on a machine that can reach your console. Replace the example console hostname and switch name, then start with a dry run.

```sh
git clone https://github.com/blakecodes/Unifi-Etherlighting-Scheduler-Linux.git
cd Unifi-Etherlighting-Scheduler-Linux

python3 unifi_etherlighting.py \
  --console https://unifi.example.net \
  --device 'My switch' \
  --username etherlighting \
  --brightness 100 \
  --dry-run
```

The script prompts for the password in your terminal. Use the exact switch name or its colon-separated MAC address. Remove `--dry-run` to apply the setting. `--brightness` accepts `1` through `100` and defaults to `100`. Use `--site` if your Network site slug differs from `default`.

### Self-signed console certificates

If the console uses a self-signed certificate, add `--cert-sha256` with its verified SHA-256 fingerprint. You can display the fingerprint presented by the console with OpenSSL. Compare it with the certificate from a trusted console connection before using it.

```sh
openssl s_client -connect unifi.example.net:443 -servername unifi.example.net </dev/null 2>/dev/null |
  openssl x509 -noout -sha256 -fingerprint
```

Pass the hexadecimal value after `=`. Colons are accepted. With a fingerprint configured, the script checks the exact certificate before sending credentials. Otherwise it uses the host's normal TLS certificate validation.

## Install the daily timer

Run these commands from the cloned repository on your Linux scheduler host.

1. Install the script and the example configuration. Edit the configuration to set your console, device, site slug, and certificate fingerprint if needed.

   ```sh
   sudo install -d -m 0755 /opt/unifi-etherlighting /etc/unifi-etherlighting
   sudo install -d -m 0700 /etc/unifi-etherlighting/credentials
   sudo install -m 0644 unifi_etherlighting.py /opt/unifi-etherlighting/
   sudo install -m 0644 systemd/config.example /etc/unifi-etherlighting/config
   sudoedit /etc/unifi-etherlighting/config
   ```

2. Store the credentials on the scheduler host. Run this block in Bash. The password prompt does not echo, and both values go to `systemd-creds` over standard input.

   ```bash
   sudo -v
   read -r -p 'Local UniFi username: ' unifi_username
   read -r -s -p 'Local UniFi password: ' unifi_password
   printf '\n'
   printf '%s' "$unifi_username" | sudo -n systemd-creds encrypt --with-key=host \
     --name=UNIFI_USERNAME - /etc/unifi-etherlighting/credentials/username
   printf '%s' "$unifi_password" | sudo -n systemd-creds encrypt --with-key=host \
     --name=UNIFI_PASSWORD - /etc/unifi-etherlighting/credentials/password
   unset unifi_username unifi_password
   sudo chmod 0600 /etc/unifi-etherlighting/credentials/{username,password}
   ```

3. Verify the scheduler can sign in and read the target switch before enabling it.

   ```sh
   sudo systemd-run --wait --pipe --collect \
     --property=DynamicUser=yes \
     --property=EnvironmentFile=/etc/unifi-etherlighting/config \
     --property=LoadCredentialEncrypted=UNIFI_USERNAME:/etc/unifi-etherlighting/credentials/username \
     --property=LoadCredentialEncrypted=UNIFI_PASSWORD:/etc/unifi-etherlighting/credentials/password \
     /usr/bin/python3 /opt/unifi-etherlighting/unifi_etherlighting.py --daily-schedule --dry-run
   ```

4. Install and enable the units, then check the next scheduled run.

   ```sh
   sudo install -m 0644 systemd/unifi-etherlighting.service \
     systemd/unifi-etherlighting.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now unifi-etherlighting.timer
   systemctl list-timers unifi-etherlighting.timer
   ```

## Operate the timer

- View scheduled run results with `journalctl -u unifi-etherlighting.service`.
- Pause automatic changes with `sudo systemctl disable --now unifi-etherlighting.timer`.
- Resume at the next scheduled time by clearing the timer's catch-up state with `sudo systemctl clean --what=state unifi-etherlighting.timer`, then running `sudo systemctl enable --now unifi-etherlighting.timer`.
- To use a different schedule, update both the `OnCalendar` entries in [the timer](systemd/unifi-etherlighting.timer) and `scheduled_brightness()` in [the script](unifi_etherlighting.py) before installing them.

## Troubleshooting

- **HTTP 499 at login** usually means UniFi requested MFA. Use a local-only console account for unattended sign-in.
- **Duplicate device names** can be resolved by using the switch's MAC address as the device selector.
- **Certificate mismatch** means the console presented a different certificate. Verify the replacement certificate before updating its fingerprint.
- **Write failed or verification timed out**. Check the service journal and run a dry run to see the controller's current value.
