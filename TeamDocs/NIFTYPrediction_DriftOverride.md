# Drift Override Retired

The drift override/overrule layer has been removed.

`DRIFT_PROBE` is now a normal production strategy. It participates independently
in the cascade like the other production `SIGNAL` strategies and is not applied
as a post-cascade override.
