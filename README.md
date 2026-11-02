# zwift-anti-cheat-detection-training-data

`make_variant_fit.py` makes a modified copy of a FIT activity file, for building
anti-cheat detection training data. It nudges power and cadence within set
margins, shifts zero runs, removes speed, changes the device name, and
recomputes lap/session summaries and CRCs. Standard library only.

```
python3 make_variant_fit.py input.fit output.fit [--seed N]
    [--power-tol PCT] [--cadence-tol PCT] [--no-offset] [--zero-shift N]
```

See the docstring at the top of the script for full details.
`fit_manufacturers.txt` lists FIT manufacturer IDs for reference.

`.fit` files are ignored by git.

## fake_power_meter.py

Makes this Mac advertise as a Bluetooth power meter (standard Cycling Power
Service), so Zwift on another machine can pair it as a Power Source and
Cadence sensor. Sends a steady wattage with small jitter, plus crank data for
cadence. The device is named "claudecode" by default (names are limited to 10
characters), close to the "claude code" label `make_variant_fit.py` uses.

```
pip install bless
python3 fake_power_meter.py [--watts 110] [--jitter 3] [--cadence 85]
    [--name claudecode] [--hz 1]
```

## nrf52_power_meter/

Firmware for an nRF52 dev board that does the same thing as
`fake_power_meter.py`, as a standalone sensor. See its README for build and
flash steps.
