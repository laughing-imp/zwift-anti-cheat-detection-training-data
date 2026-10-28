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
