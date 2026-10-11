# 0012. XVF3800 settings are applied at runtime, never saved to flash

- Status: accepted
- Date: 2026-08-22

## Decision
Register values the speaker relies on are written by `xvf-aec-ref.sh` on
every start (with retries, since the first write can race USB enumeration)
and re-checked every few minutes. `SAVE_CONFIGURATION` is never used.

## Why
They live in chip RAM and reset on power cycle or re-enumeration. A failed or
lost write is silent and total: everything looks healthy with echo
cancellation off. Keeping the values in the repo and the service, not in the
chip, makes a replaced or reflashed array behave the same.

## Related
Reading a write-only register wedges the chip ([troubleshooting](../troubleshooting.md#mic-delivers-silence));
`xvf_register.py` refuses it since 2026-10-04.
