# ATM Bank System

A thread-safe ATM bank system demonstrating concurrency concepts in Python.

## Features
- Denomination enforcement — only multiples of 20 accepted
- TOCTOU-proof withdrawals — check and deduct in one atomic lock
- Atomic transfers — debit and credit in one lock, deadlock-safe ordering
- Account locking — suspended after 3 consecutive failed attempts
- Full transaction ledger — written atomically with every balance change

## Run

```bash
python atm_bank.py
```

## Concepts Demonstrated
- Race condition prevention (threading.Lock)
- Atomicity violation fix
- Deadlock prevention via consistent lock ordering
- Starvation-aware design
EOF
