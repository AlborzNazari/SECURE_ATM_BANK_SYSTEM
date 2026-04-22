"""
+======================================================================+
|           VAULT HEIST SIMULATOR -- CONCURRENCY FLAW SUITE           |
|                                                                      |
|  A deployable, self-contained Python program that demonstrates       |
|  every major structural concurrency flaw with real output you       |
|  can observe, study, and learn from.                                 |
|                                                                      |
|  Run:  python3 vault_sim.py                                          |
|  Requires: Python 3.8+  (no external dependencies)                  |
+======================================================================+

WHAT THIS FILE TEACHES:
-----------------------
  1. TOCTOU            -- money disappears because check and act are separated
  2. Atomicity         -- data corrupted silently when logical unit split across locks
  3. Deadlock          -- system freezes due to inconsistent lock order
  4. Livelock          -- CPU burns, no progress, no priority / tiebreaker
  5. Starvation        -- one thread never runs due to no fairness policy
  6. Lazy Init Race    -- init runs multiple times, unguarded shared global
  7. Double-Checked    -- half-constructed objects, assuming construction is atomic

HOW TO READ THIS FILE:
----------------------
  Every test follows the same structure:
    1. Explanation comment  -- what the flaw IS and WHY it exists
    2. Broken version       -- code that exhibits the flaw, with comments on every line
    3. Fixed version        -- corrected code, with comments explaining each fix
    4. Runner function      -- runs both versions so you see the difference live
"""

# -*- coding: utf-8 -*-
import sys, io
# Windows terminal fix: force UTF-8 output so special chars don't crash
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import threading
import time
import random
import sys
from datetime import datetime


# ======================================================================
# UTILITIES
# Logging helpers so output is readable even when threads interleave.
# ======================================================================

# A lock just for printing -- prevents garbled interleaved output
_print_lock = threading.Lock()

def log(tag, message, symbol="-"):
    """
    Thread-safe print.
    Without this, two threads printing simultaneously produce garbage like:
      [Thread[Thread-1]-2] ] Withdrew Withdrew €100€100
    The print lock ensures each line is written atomically.
    """
    with _print_lock:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"  {ts} [{tag:<12}] {symbol} {message}")

def section(title):
    """Print a section header."""
    print(f"\n{'=' * 68}")
    print(f"  {title}")
    print(f"{'=' * 68}")

def divider(label=""):
    print(f"\n  {'-' * 30} {label} {'-' * 30}" if label else f"\n  {'-' * 64}")

def result(label, value, expected=None, good=None):
    """
    Print a result line. If expected is provided, show pass/fail.
    good=True means higher is better, good=False means lower is better.
    """
    if expected is not None:
        ok = value == expected
        mark = "[OK] PASS" if ok else "[X] FAIL"
        with _print_lock:
            print(f"  → {label}: {value}  (expected {expected})  [{mark}]")
    else:
        with _print_lock:
            print(f"  → {label}: {value}")


# ======================================================================
# FLAW 1: TOCTOU -- Time of Check to Time of Use
#
# THE CONCEPT:
#   A race condition where you read a value (CHECK), make a decision
#   based on it, then act (USE) -- but the value has changed between
#   the check and the use because another thread modified it.
#
# WHY IT HAPPENS:
#   "Check" and "act" are TWO separate operations in time.
#   The OS can pause your thread between them and run another.
#   That other thread may also check -- and also pass -- because it
#   read the same stale value before either of them acted.
#
# REAL WORLD:
#   • ATM double withdrawal (the classic)
#   • Crypto exchange double spend (what drained Mt. Gox era exchanges)
#   • File permission check then file access (Unix privesc attacks)
#   • Coupon/voucher redemption APIs (used once, redeemable N times)
# ======================================================================

def run_toctou():
    section("FLAW 1: TOCTOU -- Time-of-Check to Time-of-Use")

    # -- BROKEN VERSION ----------------------------------------------
    divider("BROKEN")
    print("  Three thieves hit a €500 vault simultaneously.")
    print("  Each one checks the balance, pauses, then withdraws.")
    print("  The pause simulates real-world latency (DB call, API, network).\n")

    vault = {"balance": 500}  # shared state -- both threads see this same dict

    def broken_withdraw(amount, name):
        # STEP 1: CHECK
        # We read vault["balance"] here.
        # This is just a read -- no lock, no protection.
        # If two threads reach this line at the same time,
        # both read the SAME value (e.g. €500).
        # Both believe they have permission to withdraw.
        current_balance = vault["balance"]

        log(name, f"checks balance: €{current_balance} -- "
                  f"{'OK to withdraw' if current_balance >= amount else 'DENIED'}")

        if current_balance >= amount:
            # THE DANGEROUS GAP
            # In production this gap is caused by: network latency, database
            # round-trip, business logic, logging, external API calls.
            # It can be microseconds or seconds -- doesn't matter.
            # During this gap, other threads have ALSO passed the check.
            # They all believe they have permission. Nobody has acted yet.
            time.sleep(0.05)  # simulates real processing delay

            # STEP 2: USE (ACT)
            # We act on the balance we read earlier.
            # But that read is now STALE -- another thread may have already
            # withdrawn. We don't re-check. We just act on the lie.
            vault["balance"] -= amount
            log(name, f"withdrew €{amount} | vault now: €{vault['balance']}", "[!]")

    # Launch three threads simultaneously -- all will pass the check
    thieves = [
        threading.Thread(target=broken_withdraw, args=(500, f"Thief-{i+1}"))
        for i in range(3)
    ]
    for t in thieves: t.start()
    for t in thieves: t.join()

    result("Final vault balance", vault["balance"], expected=0)
    # Expected: €0 (only one withdrawal should succeed)
    # Actual:   €-1000 (all three withdrew because all passed the check)

    # -- FIXED VERSION -----------------------------------------------
    divider("FIXED")
    print("  Same scenario. Now check AND act are inside the same lock.\n")

    safe_vault = {"balance": 500}
    vault_lock = threading.Lock()

    def fixed_withdraw(amount, name):
        # The lock wraps BOTH the check AND the act.
        # Only one thread can be inside this block at a time.
        # Thread B cannot even read the balance until Thread A
        # has finished checking AND withdrawing and released the lock.
        # This eliminates the gap entirely.
        with vault_lock:
            current_balance = safe_vault["balance"]  # CHECK (inside lock)
            log(name, f"checks balance: €{current_balance}")

            if current_balance >= amount:
                safe_vault["balance"] -= amount       # ACT (inside same lock)
                log(name, f"withdrew €{amount} | vault now: €{safe_vault['balance']}", "[OK]")
            else:
                log(name, f"DENIED -- only €{current_balance} available", "[X]")

    safe_thieves = [
        threading.Thread(target=fixed_withdraw, args=(500, f"Guard-{i+1}"))
        for i in range(3)
    ]
    for t in safe_thieves: t.start()
    for t in safe_thieves: t.join()

    result("Final vault balance", safe_vault["balance"], expected=0)
    # Now: only one thread gets through. Others are correctly denied.


# ======================================================================
# FLAW 2: ATOMICITY VIOLATION
#
# THE CONCEPT:
#   A "logical unit" is a group of operations that MUST all happen
#   together or not at all. A bank transfer is one logical unit:
#   (debit sender) + (credit receiver) = one transfer.
#   Splitting them into separate lock acquisitions means the system
#   can be in an INCONSISTENT STATE between the two operations.
#
# WHY IT HAPPENS:
#   Programmers lock individual operations thinking that's enough.
#   But the INVARIANT (total money is always conserved) requires
#   that both operations happen inside the SAME lock.
#
# REAL WORLD:
#   • Financial transfers with partial completion
#   • Database writes across multiple tables without a transaction
#   • Distributed systems where network failure between steps loses data
#   • Session management: logout clears token but not session cache
# ======================================================================

def run_atomicity():
    section("FLAW 2: ATOMICITY VIOLATION -- Logical unit split across locks")

    # -- BROKEN VERSION ----------------------------------------------
    divider("BROKEN")
    print("  Alice has €1000. Four transfers of €200 each.")
    print("  Transfer-2 crashes between debit and credit -- money vanishes.\n")

    accounts = {"alice": 1000, "bob": 0}
    lock = threading.Lock()

    def broken_transfer(from_acc, to_acc, amount, transfer_id):
        # LOCK 1: Debit the sender.
        # After this lock is RELEASED, alice's money is gone --
        # but bob hasn't received it yet.
        # The system is now in an INCONSISTENT STATE.
        # Total money in the system has decreased by €amount.
        # This is the invariant violation.
        with lock:
            if accounts[from_acc] >= amount:
                accounts[from_acc] -= amount
                log(f"Transfer-{transfer_id}",
                    f"debited  €{amount} from {from_acc} | {from_acc}: €{accounts[from_acc]}")
            else:
                log(f"Transfer-{transfer_id}", f"insufficient funds in {from_acc}", "[X]")
                return

        # ← THIS GAP IS THE ATOMICITY VIOLATION
        # We are between two locks. Money has left alice but not reached bob.
        # In production, this gap contains: DB commits, API calls,
        # message queue publishes, audit log writes, anything.
        # A server crash, exception, KeyboardInterrupt, OOM kill --
        # anything that stops execution here -- destroys money permanently.
        # No error. No rollback. Just silence.

        if transfer_id == 2:
            # Simulate a crash/exception between debit and credit
            log(f"Transfer-{transfer_id}",
                f"CRASH -- server killed between debit and credit!", "[!!]")
            return  # credit never happens -- €200 destroyed

        # LOCK 2: Credit the receiver.
        # Separate lock acquisition = separate transaction.
        # These are NOT atomic together with the debit above.
        with lock:
            accounts[to_acc] += amount
            log(f"Transfer-{transfer_id}",
                f"credited €{amount} to   {to_acc}   | {to_acc}: €{accounts[to_acc]}")

    transfers = [
        threading.Thread(target=broken_transfer, args=("alice", "bob", 200, i+1))
        for i in range(4)
    ]
    for t in transfers: t.start()
    for t in transfers: t.join()

    total = accounts["alice"] + accounts["bob"]
    result("alice", accounts["alice"])
    result("bob",   accounts["bob"])
    result("total in system", total, expected=1000)
    # Money should be conserved. If total < 1000, funds were destroyed.

    # -- FIXED VERSION -----------------------------------------------
    divider("FIXED")
    print("  Same scenario. Debit and credit inside ONE lock.\n")

    safe_accounts = {"alice": 1000, "bob": 0}
    safe_lock = threading.Lock()

    def fixed_transfer(from_acc, to_acc, amount, transfer_id):
        # ONE lock wraps the entire logical operation.
        # Debit and credit happen atomically -- no gap between them.
        # Either BOTH happen or NEITHER happens.
        # The invariant (total money conserved) can never be violated
        # inside this block because no other thread can observe
        # the intermediate state.
        with safe_lock:
            if safe_accounts[from_acc] >= amount:
                safe_accounts[from_acc] -= amount   # debit
                safe_accounts[to_acc]   += amount   # credit (same atomic block)
                log(f"Transfer-{transfer_id}",
                    f"moved €{amount}: {from_acc}→{to_acc} | "
                    f"{from_acc}: €{safe_accounts[from_acc]} "
                    f"{to_acc}: €{safe_accounts[to_acc]}", "[OK]")

    safe_transfers = [
        threading.Thread(target=fixed_transfer, args=("alice", "bob", 200, i+1))
        for i in range(4)
    ]
    for t in safe_transfers: t.start()
    for t in safe_transfers: t.join()

    safe_total = safe_accounts["alice"] + safe_accounts["bob"]
    result("total in system", safe_total, expected=1000)


# ======================================================================
# FLAW 3: DEADLOCK
#
# THE CONCEPT:
#   Thread A holds Lock-1 and waits for Lock-2.
#   Thread B holds Lock-2 and waits for Lock-1.
#   Neither can release what they hold. Neither can acquire what they need.
#   The system freezes -- not slowly, not with an error -- just silently
#   and permanently. No CPU usage. No output. Just a hung process.
#
# WHY IT HAPPENS:
#   Different parts of the codebase acquire the same locks in different
#   ORDER. If every piece of code always acquired Lock-1 before Lock-2,
#   Thread B would block on Lock-1 before grabbing Lock-2, and Thread A
#   could finish and release both. But inconsistent ordering creates
#   a circular dependency that cannot resolve.
#
# REAL WORLD:
#   • Database row-level locking (transaction A locks row 1 then row 2,
#     transaction B locks row 2 then row 1 -- classic DB deadlock)
#   • OS resource allocation (two processes each waiting for the other's
#     file handle, port, or device)
#   • Microservices calling each other in a cycle
# ======================================================================

def run_deadlock():
    section("FLAW 3: DEADLOCK -- System freezes, inconsistent lock order")

    # -- BROKEN VERSION ----------------------------------------------
    divider("BROKEN (with timeout to avoid hanging the demo)")
    print("  Two guards need both keycards to open a door.")
    print("  Guard-A grabs keycard-1 first. Guard-B grabs keycard-2 first.")
    print("  Each waits for the other's keycard. Forever.\n")

    keycard_1 = threading.Lock()
    keycard_2 = threading.Lock()
    deadlock_occurred = threading.Event()

    def guard_a():
        log("Guard-A", "picks up keycard-1...")
        with keycard_1:
            log("Guard-A", "has keycard-1. Waiting for keycard-2...")
            time.sleep(0.1)  # gives Guard-B time to grab keycard-2

            # In real code this would be: keycard_2.acquire()
            # which would hang forever. We use timeout for the demo.
            # In production: your server hangs. Alerts fire at 3am.
            acquired = keycard_2.acquire(timeout=0.5)
            if acquired:
                log("Guard-A", "has both keycards. Proceeding!", "[OK]")
                keycard_2.release()
            else:
                log("Guard-A", "DEADLOCK -- keycard-2 held by Guard-B forever", "[!]")
                deadlock_occurred.set()

    def guard_b():
        log("Guard-B", "picks up keycard-2...")
        with keycard_2:
            log("Guard-B", "has keycard-2. Waiting for keycard-1...")
            time.sleep(0.1)  # gives Guard-A time to grab keycard-1

            acquired = keycard_1.acquire(timeout=0.5)
            if acquired:
                log("Guard-B", "has both keycards. Proceeding!", "[OK]")
                keycard_1.release()
            else:
                log("Guard-B", "DEADLOCK -- keycard-1 held by Guard-A forever", "[!]")
                deadlock_occurred.set()

    ta = threading.Thread(target=guard_a)
    tb = threading.Thread(target=guard_b)
    ta.start(); tb.start()
    ta.join();  tb.join()

    result("Deadlock detected", deadlock_occurred.is_set(), expected=True)

    # -- FIXED VERSION -----------------------------------------------
    divider("FIXED")
    print("  Same scenario. Both guards now acquire locks in the same order.\n")

    kc_1 = threading.Lock()
    kc_2 = threading.Lock()

    def fixed_guard_a():
        log("Guard-A", "acquiring keycard-1 then keycard-2 (fixed order)...")
        with kc_1:              # always acquire 1 first
            with kc_2:          # always acquire 2 second
                log("Guard-A", "has both keycards. Proceeding!", "[OK]")

    def fixed_guard_b():
        # KEY FIX: Guard-B now also acquires 1 THEN 2.
        # When Guard-A holds kc_1, Guard-B blocks on kc_1 immediately.
        # Guard-B never grabs kc_2 before Guard-A is done with both.
        # No circular wait is possible. Deadlock eliminated structurally.
        log("Guard-B", "acquiring keycard-1 then keycard-2 (fixed order)...")
        with kc_1:              # same order: 1 first
            with kc_2:          # same order: 2 second
                log("Guard-B", "has both keycards. Proceeding!", "[OK]")

    tc = threading.Thread(target=fixed_guard_a)
    td = threading.Thread(target=fixed_guard_b)
    tc.start(); td.start()
    tc.join();  td.join()

    result("System proceeded", "YES", expected="YES")


# ======================================================================
# FLAW 4: LIVELOCK
#
# THE CONCEPT:
#   Both threads are RUNNING (not blocked like deadlock).
#   But they keep reacting to each other's state and yielding.
#   They yield simultaneously, retry simultaneously, yield again.
#   CPU usage is 100%. Zero progress. Infinite courtesy.
#
# WHY IT HAPPENS:
#   The conflict-resolution protocol has no priority or tiebreaker.
#   When both threads detect a conflict, they both back off.
#   When they both back off, the resource is free -- both try again.
#   When both try again, they both detect conflict -- both back off.
#   This cycle never terminates.
#
# REAL WORLD:
#   • Network collision protocols without exponential backoff
#     (early Ethernet had this problem -- solved by CSMA/CD with
#     randomized backoff)
#   • Message retry systems where two services keep canceling
#     each other's writes
#   • Poorly designed optimistic locking without jitter
# ======================================================================

def run_livelock():
    section("FLAW 4: LIVELOCK -- CPU burns, no progress, no tiebreaker")

    # -- BROKEN VERSION ----------------------------------------------
    divider("BROKEN")
    print("  Two processes want a shared resource.")
    print("  Both yield when they detect the other wants it.")
    print("  Both yield at the same time. CPU burns. Nothing happens.\n")

    resource_available = threading.Event()
    resource_available.set()  # resource starts free

    attempts_a = {"n": 0, "acquired": False}
    attempts_b = {"n": 0, "acquired": False}
    MAX = 8  # limit attempts so demo doesn't run forever

    def broken_process_a():
        for i in range(MAX):
            attempts_a["n"] += 1
            if resource_available.is_set():
                resource_available.clear()  # try to claim it

                # No tiebreaker -- if B also wants it, we just yield.
                # "Wanting" is approximated by checking if B's attempt
                # count matches ours (simulates simultaneous contention).
                if attempts_b["n"] >= attempts_a["n"] - 1:
                    # Detected conflict -- be polite and yield
                    resource_available.set()  # put it back
                    log("Process-A", f"attempt {i+1}: conflict detected, yielding...", "[<-]")
                    time.sleep(0.02)
                    continue  # retry -- but B is doing the same thing

                # Got it with no conflict
                log("Process-A", f"attempt {i+1}: acquired resource!", "[OK]")
                attempts_a["acquired"] = True
                time.sleep(0.05)
                resource_available.set()
                return

            time.sleep(0.01)

        log("Process-A", f"gave up after {MAX} attempts", "[X]")

    def broken_process_b():
        for i in range(MAX):
            attempts_b["n"] += 1
            if resource_available.is_set():
                resource_available.clear()

                if attempts_a["n"] >= attempts_b["n"] - 1:
                    resource_available.set()
                    log("Process-B", f"attempt {i+1}: conflict detected, yielding...", "[<-]")
                    time.sleep(0.02)
                    continue

                log("Process-B", f"attempt {i+1}: acquired resource!", "[OK]")
                attempts_b["acquired"] = True
                time.sleep(0.05)
                resource_available.set()
                return

            time.sleep(0.01)

        log("Process-B", f"gave up after {MAX} attempts", "[X]")

    ta = threading.Thread(target=broken_process_a)
    tb = threading.Thread(target=broken_process_b)
    ta.start(); tb.start()
    ta.join();  tb.join()

    livelock = not attempts_a["acquired"] and not attempts_b["acquired"]
    result("Livelock (neither acquired)", livelock)

    # -- FIXED VERSION -----------------------------------------------
    divider("FIXED")
    print("  Same scenario. Fixed with: randomized backoff + priority ID.\n")

    res = threading.Event()
    res.set()
    success = {"a": False, "b": False}

    def fixed_process(name, priority):
        """
        Two fixes applied:
        1. RANDOMIZED BACKOFF -- when conflict detected, each process
           waits a RANDOM amount of time before retrying.
           This breaks the synchronization. They stop retrying together.

        2. PRIORITY -- each process has a unique ID.
           When both detect conflict, the LOWER priority yields,
           the HIGHER priority proceeds. Clear tiebreaker. No cycle.
        """
        for i in range(10):
            if res.is_set():
                res.clear()
                time.sleep(0.01)  # brief claim window

                # Priority tiebreaker: higher priority wins
                # In practice: use process ID, timestamp, token, anything unique
                if priority == 1:  # high priority -- proceed
                    log(name, f"attempt {i+1}: high priority -- proceeding", "[OK]")
                    success[name] = True
                    time.sleep(0.05)
                    res.set()
                    return
                else:
                    # Low priority yields -- but with RANDOM backoff
                    # Random delay means they won't retry simultaneously
                    res.set()
                    backoff = random.uniform(0.01, 0.08)
                    log(name, f"attempt {i+1}: low priority, backing off {backoff:.3f}s", "[<-]")
                    time.sleep(backoff)
                    continue

            time.sleep(random.uniform(0.005, 0.015))  # jitter on wait too

    tc = threading.Thread(target=fixed_process, args=("a", 1))
    td = threading.Thread(target=fixed_process, args=("b", 2))
    tc.start(); td.start()
    tc.join();  td.join()

    result("Process-A acquired", success["a"], expected=True)


# ======================================================================
# FLAW 5: STARVATION
#
# THE CONCEPT:
#   One thread monopolizes a lock by immediately reacquiring it
#   after release. The OS scheduler, due to cache locality and
#   thread affinity, tends to give the lock back to the same thread.
#   Another thread is not blocked -- it just never wins the race
#   to acquire the lock. It is functionally invisible to the system.
#
# WHY IT HAPPENS:
#   No fairness policy. A fair lock (like a FIFO queue) would give
#   each waiting thread a turn in order. Most basic threading primitives
#   (Python's threading.Lock) make NO fairness guarantee.
#
# REAL WORLD:
#   • Web server request handling where long-running requests block
#     short ones (solved by request queuing and timeouts)
#   • Database connection pools where one query type monopolizes
#     all connections
#   • Thread pools where IO-bound tasks crowd out CPU-bound ones
# ======================================================================

def run_starvation():
    section("FLAW 5: STARVATION -- One thread never runs, no fairness policy")

    # -- BROKEN VERSION ----------------------------------------------
    divider("BROKEN")
    print("  Greedy thread holds lock, releases, immediately reacquires.")
    print("  Starving thread waits but OS keeps rescheduling greedy.")
    print("  Running for 0.8 seconds...\n")

    lock = threading.Lock()
    counts = {"greedy": 0, "starving": 0}
    stop = threading.Event()

    def greedy_thread():
        """
        This thread does real work (simulated by tiny sleep) inside the lock,
        then releases it -- but immediately tries to grab it again.
        There is NO sleep or yield between release and reacquire.
        The OS will frequently give the lock back to the same thread
        because it's already in the run queue and cache-warm.
        This is not intentionally malicious -- it's just how it works.
        """
        while not stop.is_set():
            with lock:
                counts["greedy"] += 1
                time.sleep(0.0002)  # simulates work inside lock
            # ← no sleep here -- immediately tries to reacquire
            # this single missing line causes starvation

    def starving_thread():
        """
        This thread tries to acquire the lock fairly.
        But "fairly" isn't guaranteed. The greedy thread is
        always at the front of the queue. This thread waits
        and waits and occasionally gets a turn, but far less
        than its fair share.
        """
        while not stop.is_set():
            with lock:
                counts["starving"] += 1
                time.sleep(0.0002)

    tg = threading.Thread(target=greedy_thread,   daemon=True)
    ts = threading.Thread(target=starving_thread, daemon=True)
    tg.start(); ts.start()
    time.sleep(0.8)  # observe for 0.8 seconds
    stop.set()
    tg.join(timeout=1); ts.join(timeout=1)

    total = max(counts["greedy"] + counts["starving"], 1)
    greedy_pct  = counts["greedy"]   / total * 100
    starving_pct = counts["starving"] / total * 100
    ratio = counts["greedy"] // max(counts["starving"], 1)

    result("greedy  got lock", f"{counts['greedy']:,} times  ({greedy_pct:.1f}%)")
    result("starving got lock", f"{counts['starving']:,} times ({starving_pct:.1f}%)")
    result("starvation ratio", f"{ratio}:1 (greedy vs starving)")

    # -- FIXED VERSION -----------------------------------------------
    divider("FIXED")
    print("  Same threads. Greedy thread now sleeps briefly after releasing.\n")

    fair_lock = threading.Lock()
    fair_counts = {"greedy": 0, "starving": 0}
    fair_stop = threading.Event()

    def fair_greedy():
        while not fair_stop.is_set():
            with fair_lock:
                fair_counts["greedy"] += 1
                time.sleep(0.0002)
            # THE FIX: a brief sleep after releasing gives other threads
            # a realistic chance to acquire the lock before we try again.
            # In production: use a semaphore with a FIFO queue,
            # or threading.Condition with notify(), for true fairness.
            time.sleep(0.0002)  # ← this one line prevents starvation

    def fair_starving():
        while not fair_stop.is_set():
            with fair_lock:
                fair_counts["starving"] += 1
                time.sleep(0.0002)

    fg = threading.Thread(target=fair_greedy,   daemon=True)
    fs = threading.Thread(target=fair_starving, daemon=True)
    fg.start(); fs.start()
    time.sleep(0.8)
    fair_stop.set()
    fg.join(timeout=1); fs.join(timeout=1)

    fair_total = max(fair_counts["greedy"] + fair_counts["starving"], 1)
    result("greedy  got lock", f"{fair_counts['greedy']:,}  ({fair_counts['greedy']/fair_total*100:.1f}%)")
    result("starving got lock", f"{fair_counts['starving']:,} ({fair_counts['starving']/fair_total*100:.1f}%)")
    result("balanced",
           "YES" if abs(fair_counts["greedy"] - fair_counts["starving"]) < fair_total * 0.3
           else "STILL IMBALANCED")


# ======================================================================
# FLAW 6: LAZY INITIALIZATION RACE
#
# THE CONCEPT:
#   A singleton (DB pool, config, logger, cache) is initialized
#   lazily -- only when first needed. The initialization check
#   (is it None?) is not protected by a lock.
#   Multiple threads all see None simultaneously and all initialize.
#   You end up with N instances of something that should be 1.
#
# WHY IT HAPPENS:
#   The developer thought: "it's just a None check, that's atomic."
#   A single read IS atomic. But the check-then-initialize sequence
#   is TOCTOU applied to object creation.
#
# REAL WORLD:
#   • Database connection pool created 10 times = 10x DB connections
#   • Config file parsed 10 times = 10x file handles, 10x CPU
#   • Logger initialized 10 times = duplicate log entries, corrupted files
#   • Cache warmed 10 times = 10x expensive computation on startup
# ======================================================================

def run_lazy_init():
    section("FLAW 6: LAZY INIT RACE -- Init runs multiple times, unguarded global")

    # -- BROKEN VERSION ----------------------------------------------
    divider("BROKEN")
    print("  10 threads start simultaneously. DB pool should be created once.")
    print("  All see pool=None. All initialize. Chaos.\n")

    _pool = {"instance": None}   # the shared uninitialized global
    init_count = {"n": 0}
    count_lock = threading.Lock()  # only for counting, not for init

    def expensive_init(thread_id):
        """Simulates expensive initialization: DB connection, file parse, etc."""
        time.sleep(0.05)  # simulates 50ms connection time
        return f"DBPool(conn=postgresql://localhost/prod, created_by=thread-{thread_id})"

    def broken_get_pool(thread_id):
        # This check is NOT protected.
        # All 10 threads evaluate this as True simultaneously.
        if _pool["instance"] is None:
            # All 10 enter this block.
            # All 10 call expensive_init().
            # All 10 overwrite _pool["instance"].
            # Last writer wins. All other 10 connections are leaked.
            _pool["instance"] = expensive_init(thread_id)
            with count_lock:
                init_count["n"] += 1
                log(f"Thread-{thread_id:02d}",
                    f"created pool #{init_count['n']}", "[!]")
        return _pool["instance"]

    threads = [
        threading.Thread(target=broken_get_pool, args=(i+1,))
        for i in range(10)
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    result("times initialized", init_count["n"], expected=1)

    # -- FIXED VERSION -----------------------------------------------
    divider("FIXED")
    print("  Same scenario. Check AND init inside one lock.\n")

    _safe_pool = {"instance": None}
    _init_lock = threading.Lock()
    safe_init_count = {"n": 0}

    def fixed_get_pool(thread_id):
        # Fast path: if already initialized, return immediately without lock.
        # This is safe because once set, _safe_pool["instance"] never goes
        # back to None -- reads are safe after initialization completes.
        if _safe_pool["instance"] is None:
            # Slow path: acquire lock and recheck.
            # The recheck inside the lock is MANDATORY.
            # Without it: if two threads both pass the outer None check,
            # both enter here, one initializes, releases lock,
            # the second enters and initializes AGAIN.
            with _init_lock:
                if _safe_pool["instance"] is None:  # recheck -- mandatory
                    _safe_pool["instance"] = expensive_init(thread_id)
                    safe_init_count["n"] += 1
                    log(f"Thread-{thread_id:02d}",
                        f"created pool (only once)", "[OK]")
        return _safe_pool["instance"]

    safe_threads = [
        threading.Thread(target=fixed_get_pool, args=(i+1,))
        for i in range(10)
    ]
    for t in safe_threads: t.start()
    for t in safe_threads: t.join()

    result("times initialized", safe_init_count["n"], expected=1)


# ======================================================================
# FLAW 7: DOUBLE-CHECKED LOCKING -- Half-constructed objects
#
# THE CONCEPT:
#   An "optimization" that tries to avoid locking after initialization.
#   But it assumes object construction is atomic. It never is.
#   Object creation has three phases:
#     1. Allocate memory (pointer is now non-None)
#     2. Assign the pointer to the variable  ← another thread sees non-None
#     3. Run the constructor (initialize fields) ← but fields aren't set yet!
#   Thread B sees a non-None pointer at step 2 and returns the object.
#   But the object's fields haven't been initialized yet (step 3 incomplete).
#   Thread B uses a half-constructed object -- fields are None, zero, garbage.
#
# WHY IT MATTERS IN PYTHON:
#   Python's GIL makes this LESS dangerous than in Java/C++, but the
#   pattern is still fundamentally broken in principle, and matters for:
#   • Jython (no GIL)
#   • PyPy with STM
#   • Any language without a GIL (Java, C++, Go, Rust unsafe)
#   • Any multi-process Python code
#
# REAL WORLD (Java/C++):
#   The most famous concurrency anti-pattern.
#   Appeared in every Java textbook as "the clever optimization."
#   Broke countless production systems before Java 5's memory model
#   added the volatile keyword to fix it.
# ======================================================================

def run_double_checked_lock():
    section("FLAW 7: DOUBLE-CHECKED LOCKING -- Half-constructed objects")

    # -- BROKEN VERSION ----------------------------------------------
    divider("BROKEN -- demonstrating the pattern flaw")
    print("  Simulating double-checked locking with artificial construction delay.")
    print("  Thread B sees instance != None but fields not yet initialized.\n")

    class BrokenConfig:
        """
        A config object that takes time to fully initialize.
        Simulates: reading config file, connecting to DB, loading keys.
        """
        def __init__(self, thread_id):
            # Phase 1: object exists (memory allocated, pointer assigned)
            # At this exact moment, another thread doing the outer None check
            # sees self as non-None and returns this half-built object.
            self.initialized = False  # ← not set to True yet
            self.db_url = None        # ← None until initialization completes
            self.api_key = None       # ← None until initialization completes

            # Simulate time-consuming initialization
            time.sleep(0.05)          # ← during this sleep, object is incomplete

            # Phase 2: fully initialized
            self.db_url  = f"postgresql://prod-server/main"
            self.api_key = f"sk-live-{thread_id}-{'x'*32}"
            self.initialized = True   # ← only True after ALL fields are set

    _broken_instance = {"ref": None}
    _broken_lock = threading.Lock()
    observations = []
    obs_lock = threading.Lock()

    def broken_get_config(thread_id):
        # OUTER CHECK -- no lock (this is intentional in this pattern)
        if _broken_instance["ref"] is None:
            with _broken_lock:
                # INNER CHECK -- with lock
                if _broken_instance["ref"] is None:
                    # Start constructing -- but Python dict assignment here
                    # assigns the PARTIALLY constructed object.
                    # In Java/C++: the pointer is visible to other threads
                    # BEFORE the constructor finishes due to memory reordering.
                    _broken_instance["ref"] = BrokenConfig(thread_id)

        # Thread B arrives here when ref is not None but may be mid-construction
        config = _broken_instance["ref"]
        with obs_lock:
            observations.append({
                "thread": thread_id,
                "initialized": config.initialized,
                "has_db_url":  config.db_url is not None,
                "has_api_key": config.api_key is not None,
            })

        return config

    # Thread 1 starts constructing. Thread 2 starts 10ms later.
    # Thread 2 may see ref != None but construction not complete.
    results_store = {}

    def runner(tid, delay):
        time.sleep(delay)
        cfg = broken_get_config(tid)
        results_store[tid] = cfg

    t1 = threading.Thread(target=runner, args=(1, 0))
    t2 = threading.Thread(target=runner, args=(2, 0.01))  # starts mid-construction
    t1.start(); t2.start()
    t1.join();  t2.join()

    for obs in observations:
        log(f"Thread-{obs['thread']}",
            f"initialized={obs['initialized']}  "
            f"db_url={'SET' if obs['has_db_url'] else 'NONE'}  "
            f"api_key={'SET' if obs['has_api_key'] else 'NONE'}")

    # -- FIXED VERSION -----------------------------------------------
    divider("FIXED")
    print("  Fix 1: Always lock. Accept the tiny performance cost.")
    print("  Fix 2: Module-level initialization (Python-idiomatic).\n")

    class SafeConfig:
        def __init__(self):
            time.sleep(0.05)  # same expensive init
            self.db_url  = "postgresql://prod-server/main"
            self.api_key = "sk-live-fully-constructed"
            self.initialized = True

    _safe_instance = {"ref": None}
    _safe_lock = threading.Lock()

    def safe_get_config(thread_id):
        # FIX: always lock. No outer unprotected check.
        # Yes this costs one lock acquisition per call after initialization.
        # In Python: this cost is ~100ns. Your DB query costs ~5ms.
        # The "optimization" that double-checked locking provides
        # is meaningless at this scale and dangerous at the language level.
        with _safe_lock:
            if _safe_instance["ref"] is None:
                _safe_instance["ref"] = SafeConfig()
                log(f"Thread-{thread_id:02d}", "constructed SafeConfig", "[OK]")

        config = _safe_instance["ref"]
        log(f"Thread-{thread_id:02d}",
            f"initialized={config.initialized}  "
            f"db_url={'SET' if config.db_url else 'NONE'}  "
            f"api_key={'SET' if config.api_key else 'NONE'}", "[OK]")
        return config

    s1 = threading.Thread(target=safe_get_config, args=(1,))
    s2 = threading.Thread(target=safe_get_config, args=(2,))
    s1.start(); s2.start()
    s1.join();  s2.join()

    result("both see fully initialized object",
           all(o["initialized"] for o in observations))


# ======================================================================
# FINAL SUMMARY
# ======================================================================

def print_summary():
    section("SUMMARY -- What Every Senior Engineer Internalizes")
    print("""
  +-------------------------+------------------------------+-----------------------------+
  | Flaw                    | Root cause                   | Fix                         |
  +-------------------------+------------------------------+-----------------------------+
  | TOCTOU                  | Check and act separated       | Lock wraps check + act      |
  | Atomicity violation     | Logical unit split across     | One lock for one operation  |
  |                         | multiple lock boundaries      |                             |
  | Deadlock                | Inconsistent lock order       | Always same order globally  |
  | Livelock                | No tiebreaker, both yield     | Randomized backoff+priority |
  | Starvation              | No fairness, greedy reacquire | Yield between acquisitions  |
  | Lazy init race          | Unguarded None check          | Lock wraps check + init     |
  | Double-checked lock     | Construction not atomic       | Always lock, or init early  |
  +-------------------------+------------------------------+-----------------------------+

  THE ONE RULE BEHIND ALL OF THEM:
  ---------------------------------
  Never let two threads touch shared state at the same time
  without a rule that enforces they take turns for the ENTIRE
  logical operation -- not just part of it.

  A "logical operation" is the minimal unit that must complete
  atomically for your invariant (your correctness condition) to hold.

  If your invariant is "total money is conserved":
    → debit + credit is your logical operation
    → both must be inside one lock

  If your invariant is "singleton is constructed exactly once":
    → check + construct is your logical operation
    → both must be inside one lock

  Find your invariant. Find the operations that touch it.
  Wrap them. That's it.
    """)


# ======================================================================
# ENTRY POINT
# ======================================================================

if __name__ == "__main__":
    print("""
+======================================================================+
|           VAULT HEIST SIMULATOR -- CONCURRENCY FLAW SUITE           |
|        7 structural flaws - broken + fixed - live output            |
+======================================================================+
    """)

    # You can run individual tests or all of them
    if len(sys.argv) > 1:
        tests = {
            "toctou":     run_toctou,
            "atomicity":  run_atomicity,
            "deadlock":   run_deadlock,
            "livelock":   run_livelock,
            "starvation": run_starvation,
            "lazyinit":   run_lazy_init,
            "dclk":       run_double_checked_lock,
        }
        name = sys.argv[1].lower()
        if name in tests:
            tests[name]()
        else:
            print(f"  Unknown test: {name}")
            print(f"  Available: {', '.join(tests.keys())}")
        sys.exit(0)

    # Run all tests
    run_toctou()
    run_atomicity()
    run_deadlock()
    run_livelock()
    run_starvation()
    run_lazy_init()
    run_double_checked_lock()
    print_summary()
