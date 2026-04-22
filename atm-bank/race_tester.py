"""
CONCURRENCY VULNERABILITY TESTER
=================================
Tests 6 structural flaws in concurrent systems:
1. TOCTOU      - Time of Check to Time of Use
2. Atomicity   - Operations that must be one unit but aren't
3. Deadlock    - Two threads waiting for each other forever
4. Livelock    - Both moving, neither progressing
5. Starvation  - One thread never gets scheduled
6. Lazy Init   - Singleton created multiple times
"""

import threading
import time
import random
import json
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# RESULT COLLECTOR
# Collects results from all tests so we can display them later
# ─────────────────────────────────────────────────────────────
results = {}

def record(test_name, data):
    """Store test results for final report"""
    results[test_name] = {
        "timestamp": datetime.now().isoformat(),
        **data
    }

# ═══════════════════════════════════════════════════════════════
# TEST 1: TOCTOU — Time of Check to Time of Use
# ═══════════════════════════════════════════════════════════════
# The flaw: you CHECK a condition (is there money?),
# then ACT on it (withdraw money).
# Between check and act, another thread can change the world.
# Your check is now stale. You're acting on a lie.
def test_toctou():
    print("\n" + "═"*60)
    print("TEST 1: TOCTOU (Time-of-Check-to-Time-of-Use)")
    print("═"*60)
    print("Scenario: Bank vault with €500. Three thieves withdraw simultaneously.")
    print("Each checks balance first, then waits, then withdraws.")
    print("The wait simulates real-world network/DB latency.\n")

    vault = {"balance": 500}
    log = []

    def flawed_withdraw(amount, name):
        # STEP 1: CHECK — reads the current balance
        # This is the "Time of Check"
        current = vault["balance"]
        print(f"  [{name}] checks balance: €{current} — {'sufficient ✓' if current >= amount else 'insufficient ✗'}")

        if current >= amount:
            # THE DANGEROUS GAP
            # In a real system this gap is: network latency, DB query,
            # business logic processing, API call — anything takes time.
            # During this gap, other threads are also past the check.
            # They ALL saw sufficient funds. They ALL think they have permission.
            time.sleep(0.05)  # simulates processing delay

            # STEP 2: ACT — this is "Time of Use"
            # By now, other threads may have already withdrawn.
            # But we don't re-check. We just act on our stale read.
            vault["balance"] -= amount
            msg = f"  [{name}] withdrew €{amount} | New balance: €{vault['balance']}"
            print(msg)
            log.append({"actor": name, "withdrew": amount, "balance_after": vault["balance"]})

    threads = [
        threading.Thread(target=flawed_withdraw, args=(500, f"Thief-{i+1}"))
        for i in range(3)
    ]

    for t in threads: t.start()
    for t in threads: t.join()

    final = vault["balance"]
    expected = 0  # only one withdrawal should succeed
    vulnerable = final < expected

    print(f"\n  Expected final balance: €{expected} (only one withdrawal)")
    print(f"  Actual final balance:   €{final}")
    print(f"  VULNERABILITY: {'YES — vault drained below zero ⚠' if vulnerable else 'no race this run'}")

    record("toctou", {
        "expected_balance": expected,
        "actual_balance": final,
        "vulnerable": vulnerable,
        "withdrawals": log,
        "explanation": "Check and Act are separated in time. All threads pass the check before any writes back."
    })

# ═══════════════════════════════════════════════════════════════
# TEST 2: ATOMICITY VIOLATION
# ═══════════════════════════════════════════════════════════════
# The flaw: a transfer is ONE logical operation (debit + credit).
# But the code splits it into TWO separate locked sections.
# Between debit and credit, the system is in an INCONSISTENT STATE.
# Points have left one account but not yet arrived in the other.
# If anything interrupts here — crash, exception, network drop —
# those points simply vanish. No error. No recovery.
def test_atomicity():
    print("\n" + "═"*60)
    print("TEST 2: ATOMICITY VIOLATION")
    print("═"*60)
    print("Scenario: Points transfer system. Transfer must debit AND credit atomically.")
    print("Broken version splits debit and credit into separate lock acquisitions.")
    print("The window between them is where corruption lives.\n")

    accounts = {"alice": 1000, "bob": 0}
    lock = threading.Lock()
    interrupted_transfers = []

    def broken_transfer(from_acc, to_acc, amount, transfer_id):
        # LOCK 1: debit the sender
        # After this lock releases, alice's money is gone
        # but bob hasn't received it yet.
        # The system is now in an INCONSISTENT state.
        with lock:
            if accounts[from_acc] >= amount:
                accounts[from_acc] -= amount
                print(f"  [Transfer-{transfer_id}] Debited €{amount} from {from_acc} | {from_acc}: €{accounts[from_acc]}")

        # ← RIGHT HERE: money is in neither account
        # This gap is the atomicity violation.
        # In production this could be: a DB commit, an API call,
        # a message queue publish, a file write.
        # Any failure here = money destroyed.

        # Simulate a crash happening between debit and credit
        if transfer_id == 2:
            print(f"  [Transfer-{transfer_id}] ⚡ CRASH between debit and credit!")
            interrupted_transfers.append(transfer_id)
            return  # credit never happens

        # LOCK 2: credit the receiver (separate lock — separate transaction)
        with lock:
            accounts[to_acc] += amount
            print(f"  [Transfer-{transfer_id}] Credited €{amount} to {to_acc}   | {to_acc}: €{accounts[to_acc]}")

    threads = [
        threading.Thread(target=broken_transfer, args=("alice", "bob", 200, i+1))
        for i in range(4)
    ]

    for t in threads: t.start()
    for t in threads: t.join()

    total = accounts["alice"] + accounts["bob"]
    expected_total = 1000  # money should be conserved
    money_destroyed = expected_total - total

    print(f"\n  Alice: €{accounts['alice']} | Bob: €{accounts['bob']}")
    print(f"  Total in system: €{total} (should be €{expected_total})")
    print(f"  Money destroyed: €{money_destroyed}")
    print(f"  VULNERABILITY: {'YES — funds lost to interrupted transfer ⚠' if money_destroyed > 0 else 'lucky run'}")

    record("atomicity", {
        "alice_final": accounts["alice"],
        "bob_final": accounts["bob"],
        "total_in_system": total,
        "expected_total": expected_total,
        "money_destroyed": money_destroyed,
        "vulnerable": money_destroyed > 0,
        "explanation": "Debit and Credit are in separate lock sections. Crash between them destroys funds."
    })

# ═══════════════════════════════════════════════════════════════
# TEST 3: DEADLOCK
# ═══════════════════════════════════════════════════════════════
# The flaw: Thread A holds Lock 1, wants Lock 2.
#           Thread B holds Lock 2, wants Lock 1.
# Neither can proceed. Neither releases. System freezes.
# The flaw is in lock ORDERING — inconsistent across threads.
def test_deadlock():
    print("\n" + "═"*60)
    print("TEST 3: DEADLOCK")
    print("═"*60)
    print("Scenario: Two security doors, each needing two keycards.")
    print("Guard A takes keycard-1 then needs keycard-2.")
    print("Guard B takes keycard-2 then needs keycard-1.")
    print("Both wait forever. Building is locked. Nobody gets in.\n")

    keycard_1 = threading.Lock()
    keycard_2 = threading.Lock()
    deadlock_detected = threading.Event()
    results_log = []

    def guard_a():
        print("  [Guard-A] Picks up keycard-1...")
        with keycard_1:
            print("  [Guard-A] Has keycard-1. Needs keycard-2...")
            # Give Guard B time to grab keycard-2
            time.sleep(0.1)
            # Try to get keycard-2 with timeout (to detect deadlock)
            # In real code there's no timeout — it hangs forever
            acquired = keycard_2.acquire(timeout=0.5)
            if acquired:
                print("  [Guard-A] Has both keycards! Proceeding.")
                keycard_2.release()
                results_log.append("guard_a_succeeded")
            else:
                print("  [Guard-A] DEADLOCK — cannot get keycard-2. Guard-B has it.")
                results_log.append("guard_a_deadlocked")
                deadlock_detected.set()

    def guard_b():
        print("  [Guard-B] Picks up keycard-2...")
        with keycard_2:
            print("  [Guard-B] Has keycard-2. Needs keycard-1...")
            time.sleep(0.1)
            acquired = keycard_1.acquire(timeout=0.5)
            if acquired:
                print("  [Guard-B] Has both keycards! Proceeding.")
                keycard_1.release()
                results_log.append("guard_b_succeeded")
            else:
                print("  [Guard-B] DEADLOCK — cannot get keycard-1. Guard-A has it.")
                results_log.append("guard_b_deadlocked")
                deadlock_detected.set()

    ta = threading.Thread(target=guard_a)
    tb = threading.Thread(target=guard_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    deadlocked = deadlock_detected.is_set()
    print(f"\n  Deadlock occurred: {'YES ⚠' if deadlocked else 'no — lucky timing'}")
    print(f"  Root cause: Inconsistent lock ordering (A takes 1→2, B takes 2→1)")
    print(f"  Fix: Always acquire locks in the same order everywhere in the codebase")

    record("deadlock", {
        "deadlock_detected": deadlocked,
        "results": results_log,
        "explanation": "Inconsistent lock order. A waits for B's lock, B waits for A's lock. Circular wait."
    })

# ═══════════════════════════════════════════════════════════════
# TEST 4: LIVELOCK
# ═══════════════════════════════════════════════════════════════
# The flaw: Both threads keep responding to each other's state.
# Both are ACTIVE — not blocked — but making ZERO progress.
# Like two people in a corridor both stepping aside simultaneously,
# forever, in perfect synchronization.
def test_livelock():
    print("\n" + "═"*60)
    print("TEST 4: LIVELOCK")
    print("═"*60)
    print("Scenario: Two processes both want a shared resource.")
    print("Both are 'polite' — they yield when they detect conflict.")
    print("But they yield at the same time, forever. CPU burns. Nothing happens.\n")

    resource_free = threading.Event()
    resource_free.set()  # resource starts as available

    progress_a = {"count": 0, "acquired": False}
    progress_b = {"count": 0, "acquired": False}
    max_attempts = 8

    def process_a():
        for attempt in range(max_attempts):
            # Check if resource is available
            if resource_free.is_set():
                # Try to claim it
                resource_free.clear()
                time.sleep(0.02)

                # Check if B also tried to grab it (simulated conflict)
                if progress_b["count"] == attempt:
                    # Detected conflict — be polite, yield
                    resource_free.set()  # give it back
                    progress_a["count"] += 1
                    print(f"  [Process-A] Attempt {attempt+1}: yielding to B (polite)")
                    time.sleep(random.uniform(0.01, 0.03))
                    continue

                # Got it!
                print(f"  [Process-A] Attempt {attempt+1}: acquired resource!")
                progress_a["acquired"] = True
                time.sleep(0.05)
                resource_free.set()
                return

            progress_a["count"] += 1
            time.sleep(0.01)

        print(f"  [Process-A] Gave up after {max_attempts} attempts — never acquired")

    def process_b():
        for attempt in range(max_attempts):
            if resource_free.is_set():
                resource_free.clear()
                time.sleep(0.02)

                if progress_a["count"] == attempt:
                    resource_free.set()
                    progress_b["count"] += 1
                    print(f"  [Process-B] Attempt {attempt+1}: yielding to A (polite)")
                    time.sleep(random.uniform(0.01, 0.03))
                    continue

                print(f"  [Process-B] Attempt {attempt+1}: acquired resource!")
                progress_b["acquired"] = True
                time.sleep(0.05)
                resource_free.set()
                return

            progress_b["count"] += 1
            time.sleep(0.01)

        print(f"  [Process-B] Gave up after {max_attempts} attempts — never acquired")

    ta = threading.Thread(target=process_a)
    tb = threading.Thread(target=process_b)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    neither_acquired = not progress_a["acquired"] and not progress_b["acquired"]
    print(f"\n  Process-A acquired: {progress_a['acquired']}")
    print(f"  Process-B acquired: {progress_b['acquired']}")
    print(f"  Livelock occurred: {'YES — both kept yielding ⚠' if neither_acquired else 'one succeeded'}")
    print(f"  Fix: Add randomized backoff + priority ordering between processes")

    record("livelock", {
        "process_a_acquired": progress_a["acquired"],
        "process_b_acquired": progress_b["acquired"],
        "livelock_detected": neither_acquired,
        "explanation": "Both threads actively yield to each other simultaneously. CPU burns, no progress."
    })

# ═══════════════════════════════════════════════════════════════
# TEST 5: STARVATION
# ═══════════════════════════════════════════════════════════════
# The flaw: One thread monopolizes a lock by immediately
# re-acquiring it after release. The OS tends to give the lock
# back to the same thread (cache locality, scheduling bias).
# Another thread waits forever — not blocked, just never scheduled.
def test_starvation():
    print("\n" + "═"*60)
    print("TEST 5: STARVATION")
    print("═"*60)
    print("Scenario: CPU-intensive process hogs a lock.")
    print("Low-priority thread waits but OS keeps rescheduling the greedy thread.")
    print("The waiting thread is starved of CPU time.\n")

    lock = threading.Lock()
    counts = {"greedy": 0, "starving": 0}
    stop = threading.Event()

    def greedy():
        # Acquires lock, does work, releases, immediately grabs again.
        # OS scheduling bias tends to give lock back to same thread.
        # This isn't malicious — it's just how scheduling works.
        # But the effect is starvation for other threads.
        while not stop.is_set():
            with lock:
                counts["greedy"] += 1
                # Tiny sleep simulates actual work
                # Without sleep it would be even more greedy
                time.sleep(0.0001)
            # NO sleep outside lock — immediately tries to reacquire
            # This is the structural flaw: no yield between acquisitions

    def starving():
        # Tries to get the lock but greedy thread always gets there first
        attempts = 0
        while not stop.is_set():
            acquired = lock.acquire(timeout=0.01)
            if acquired:
                counts["starving"] += 1
                time.sleep(0.0001)
                lock.release()
            attempts += 1

    tg = threading.Thread(target=greedy)
    ts = threading.Thread(target=starving)

    tg.start()
    ts.start()
    time.sleep(1.0)  # let them run for 1 second
    stop.set()
    tg.join()
    ts.join()

    total = counts["greedy"] + counts["starving"]
    greedy_pct  = counts["greedy"]   / total * 100 if total > 0 else 0
    starving_pct = counts["starving"] / total * 100 if total > 0 else 0

    print(f"  Greedy thread ran:   {counts['greedy']:,} times ({greedy_pct:.1f}%)")
    print(f"  Starving thread ran: {counts['starving']:,} times ({starving_pct:.1f}%)")
    print(f"  Starvation ratio: {counts['greedy'] // max(counts['starving'],1)}:1")
    print(f"  VULNERABILITY: {'YES — severe scheduling imbalance ⚠' if greedy_pct > 80 else 'acceptable balance'}")
    print(f"  Fix: Add sleep/yield between acquisitions, use fair queuing locks")

    record("starvation", {
        "greedy_count": counts["greedy"],
        "starving_count": counts["starving"],
        "greedy_percentage": round(greedy_pct, 1),
        "starvation_ratio": counts["greedy"] // max(counts["starving"], 1),
        "vulnerable": greedy_pct > 80,
        "explanation": "Greedy thread reacquires lock immediately. OS scheduling bias starves others."
    })

# ═══════════════════════════════════════════════════════════════
# TEST 6: LAZY INITIALIZATION RACE
# ═══════════════════════════════════════════════════════════════
# The flaw: A singleton is initialized lazily (on first use).
# Multiple threads all see it as None simultaneously.
# All of them initialize it. You get multiple instances
# of something that should only ever exist once:
# DB connection pools, config loaders, loggers, caches.
def test_lazy_init():
    print("\n" + "═"*60)
    print("TEST 6: LAZY INITIALIZATION RACE")
    print("═"*60)
    print("Scenario: Database connection pool — should be created ONCE.")
    print("10 threads start simultaneously, all see pool=None.")
    print("All create their own pool. Now you have 10 DB connections\n"
          "  instead of 1 shared pool. Memory leak. Connection exhaustion.\n")

    # The shared singleton — starts as None
    _db_pool = {"instance": None}
    init_count = {"value": 0}
    init_log = []
    log_lock = threading.Lock()

    def broken_get_pool(thread_id):
        # This check is NOT protected by a lock.
        # All 10 threads read None at the same time.
        # All 10 enter the if block.
        # All 10 create a new pool.
        if _db_pool["instance"] is None:
            # Simulate expensive initialization (connecting to DB)
            time.sleep(0.05)
            _db_pool["instance"] = f"DBPool-created-by-thread-{thread_id}"

            with log_lock:
                init_count["value"] += 1
                init_log.append(f"Thread-{thread_id} created pool")
                print(f"  [Thread-{thread_id:2d}] Created DB pool (init #{init_count['value']})")

        return _db_pool["instance"]

    threads = [
        threading.Thread(target=broken_get_pool, args=(i+1,))
        for i in range(10)
    ]

    for t in threads: t.start()
    for t in threads: t.join()

    times_initialized = init_count["value"]
    print(f"\n  Pool initialized: {times_initialized} times (should be exactly 1)")
    print(f"  VULNERABILITY: {'YES — multiple pools created ⚠' if times_initialized > 1 else 'lucky — only 1'}")
    print(f"  Fix: Lock the entire check+init block, or use module-level initialization")

    record("lazy_init", {
        "times_initialized": times_initialized,
        "vulnerable": times_initialized > 1,
        "init_log": init_log,
        "explanation": "Unguarded None check. All threads see None simultaneously. All initialize."
    })

# ═══════════════════════════════════════════════════════════════
# FINAL REPORT
# ═══════════════════════════════════════════════════════════════
def print_report():
    print("\n\n" + "═"*60)
    print("VULNERABILITY SCAN REPORT")
    print("═"*60)

    vuln_map = {
        "toctou":     "TOCTOU (Check-Then-Act)",
        "atomicity":  "Atomicity Violation",
        "deadlock":   "Deadlock",
        "livelock":   "Livelock",
        "starvation": "Starvation",
        "lazy_init":  "Lazy Init Race",
    }

    total_vulnerable = 0
    for key, label in vuln_map.items():
        if key in results:
            r = results[key]
            vuln = r.get("vulnerable", r.get("deadlock_detected", r.get("livelock_detected", False)))
            status = "VULNERABLE ⚠" if vuln else "SAFE ✓"
            if vuln: total_vulnerable += 1
            print(f"  {label:<35} {status}")

    print(f"\n  Total vulnerabilities found: {total_vulnerable}/6")
    print("═"*60)

    # Save full results to JSON
    with open("/mnt/user-data/outputs/scan_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("  Full results saved to: scan_results.json")

# ═══════════════════════════════════════════════════════════════
# RUN ALL TESTS
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("CONCURRENCY VULNERABILITY TESTER")
    print("Testing 6 structural flaws in concurrent systems")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    test_toctou()
    test_atomicity()
    test_deadlock()
    test_livelock()
    test_starvation()
    test_lazy_init()
    print_report()
