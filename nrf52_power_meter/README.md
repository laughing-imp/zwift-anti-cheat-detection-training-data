# nrf52_power_meter

Zephyr / nRF Connect SDK app that turns an nRF52 dev board into a BLE power
meter. It advertises the standard Cycling Power Service (0x1818) as
"Garmin", so Zwift can pair it as a Power Source and Cadence sensor. It
sends 110 W (+/-3) and 85 rpm crank data once a second. The Device Information
Service reports manufacturer "Garmin", model "nrf52 power sim".

LED0 blinks while advertising and stays on while connected. Logs go to the
board's USB serial port (115200).

## Build

Uses the west workspace in `~/code/tms-fw` (Zephyr 4.4, NCS, Zephyr SDK 0.17):

```bash
cd ~/code/tms-fw && source .nrf/bin/activate
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr ZEPHYR_SDK_INSTALL_DIR=$HOME/zephyr-sdk-0.17.0
APP=~/code/zwift-anti-cheat-detection-training-data/nrf52_power_meter

# nRF52840 DK
west build -p always -b nrf52840dk/nrf52840 -s $APP -d $APP/build
# nRF52832 DK
west build -p always -b nrf52dk/nrf52832 -s $APP -d $APP/build_nrf52832
# Fanstel EV-WT02C40C (nRF5340, board files live in tms-fw)
west build -p always -b ev_wt02c40c/nrf5340/cpuapp -s $APP -d $APP/build_wt02c40c \
  -- -DBOARD_ROOT=$HOME/code/tms-fw/tms-fw
```

On the nRF5340 the build makes two images: the app, and `ipc_radio` (the
Bluetooth controller) for the network core. `west flash` programs both. The
EV-WT02C40C has no LEDs; its console is the native micro-USB port (CDC ACM).

Change the values at build time, e.g.:

```bash
west build -b nrf52840dk/nrf52840 -s $APP -d $APP/build -- \
  -DCONFIG_APP_WATTS=150 -DCONFIG_APP_CADENCE_RPM=90
```

Options: `CONFIG_APP_WATTS`, `CONFIG_APP_JITTER_WATTS`, `CONFIG_APP_CADENCE_RPM`
(0 = power only), `CONFIG_APP_UPDATE_MS`.

## Flash

nRF52 DKs: plug in the DK's debug USB port (the one labelled for J-Link),
then `west flash -d $APP/build`.

EV-WT02C40C: it has no on-board debugger. Connect an nRF5340 DK's
*Debug out* to the board's *Debug in* header, plug the DK into USB, then:

```bash
west flash -d $APP/build_wt02c40c
```

### Flash the prebuilt EV-WT02C40C firmware (no build needed)

`firmware/ev_wt02c40c/` holds ready-to-flash images built from this source
with the default settings (170 W base, buttons step 10 W):

- `net_core_ipc_radio.hex`: Bluetooth controller for the network core
- `app.hex`: the power meter app for the application core

Install [nrfutil](https://www.nordicsemi.com/Products/Development-tools/nRF-Util)
and its device command (`nrfutil install device`). Wire up the nRF5340 DK as
above, then from this folder:

```bash
nrfutil device program --firmware firmware/ev_wt02c40c/net_core_ipc_radio.hex \
  --core Network --options chip_erase_mode=ERASE_RANGES_TOUCHED_BY_FIRMWARE
nrfutil device program --firmware firmware/ev_wt02c40c/app.hex \
  --core Application --options chip_erase_mode=ERASE_RANGES_TOUCHED_BY_FIRMWARE
nrfutil device reset
```

Flash the network core first. If more than one board is plugged in, add
`--serial-number <DK serial>` to each command (`nrfutil device list` shows it).

After a source change, rebuild and copy the new images over these files:

```bash
cp build_wt02c40c/nrf52_power_meter/zephyr/zephyr.hex firmware/ev_wt02c40c/app.hex
cp build_wt02c40c/ipc_radio/zephyr/zephyr.hex firmware/ev_wt02c40c/net_core_ipc_radio.hex
```

## Buttons (EV-WT02C40C)

Button 1 raises the base power by `CONFIG_APP_BUTTON_STEP_WATTS` (default
10 W) and button 2 lowers it, within `CONFIG_APP_MIN_WATTS` and
`CONFIG_APP_MAX_WATTS`. Each press logs the new value on the USB console.
ERG targets from Zwift still take priority. The value resets on reboot.
