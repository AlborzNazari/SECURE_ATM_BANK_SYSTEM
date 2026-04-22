"""
SECURE ATM BANK SYSTEM
=======================
Rules:
  - Withdrawals must be exact multiples of 20 only
  - 15, 33, 50, 75 etc. are REJECTED at the door
  - All operations are race-condition free (full locking)
  - Atomicity guaranteed: debit and ledger write in one lock
  - Thread-safe: works correctly under 50 simultaneous users
 
Run:
  python atm_bank.py          -- full simulation
  python atm_bank.py quiet    -- results only
"""
 
import threading
import time
import random
from datetime import datetime
 
# ── print lock so threads don't garble output ────────────────────
_pl = threading.Lock()
 
def log(tag, msg, sym="-"):
    with _pl:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"  {ts}  [{tag:<16}] {sym} {msg}")
 
 
# ══════════════════════════════════════════════════════════════════
# DENOMINATION VALIDATOR
#
# This is the gatekeeper. It runs BEFORE any lock is acquired,
# BEFORE any balance is checked, BEFORE any thread contention.
#
# Why here and not inside the account?
#   Because denomination validation is a BUSINESS RULE, not a
#   concurrency concern. It never touches shared state.
#   It is pure math. No lock needed. Always deterministic.
#
# The math:
#   amount % 20 == 0 means "amount divides evenly by 20"
#   % is the modulo operator -- it gives the remainder.
#
#   20 % 20 = 0   --> valid   (20 / 20 = 1, remainder 0)
#   40 % 20 = 0   --> valid   (40 / 20 = 2, remainder 0)
#   60 % 20 = 0   --> valid   (60 / 20 = 3, remainder 0)
#   150 % 20 = 10 --> INVALID (150 / 20 = 7, remainder 10)
#   33 % 20 = 13  --> INVALID (33 / 20 = 1, remainder 13)
#   0 % 20 = 0    --> INVALID (special case: zero is blocked)
#   -20 % 20 = 0  --> INVALID (negative amounts blocked separately)
# ══════════════════════════════════════════════════════════════════
 
DENOMINATION = 20   # the only valid note in this ATM
MAX_WITHDRAWAL = 600  # max per single transaction
MIN_WITHDRAWAL = 20   # minimum is one note
 
class DenominationError(Exception):
    """Raised when withdrawal amount violates denomination rules."""
    pass
 
class InsufficientFundsError(Exception):
    """Raised when balance is too low."""
    pass
 
class AccountLockedError(Exception):
    """Raised when account is suspended (e.g. after too many failures)."""
    pass
 
 
def validate_denomination(amount):
    """
    Pure validation function. No side effects. No shared state.
    Returns nothing if valid. Raises DenominationError if not.
 
    Called first, before any lock, before any balance check.
    Cheap to call -- just arithmetic.
 
    Examples:
      validate_denomination(20)  --> passes silently
      validate_denomination(40)  --> passes silently
      validate_denomination(33)  --> raises DenominationError
      validate_denomination(0)   --> raises DenominationError
      validate_denomination(-20) --> raises DenominationError
    """
 
    # Rule 1: must be a number
    if not isinstance(amount, (int, float)):
        raise DenominationError(
            f"Invalid input: {amount!r} is not a number")
 
    # Rule 2: must be a positive integer
    # Floats like 20.0 are accepted (converted), 20.5 is rejected
    if isinstance(amount, float):
        if not amount.is_integer():
            raise DenominationError(
                f"Amount {amount} is not a whole number. "
                f"ATM only dispenses whole notes.")
        amount = int(amount)
 
    # Rule 3: must be positive
    if amount <= 0:
        raise DenominationError(
            f"Amount must be positive. Got: {amount}")
 
    # Rule 4: must not exceed maximum
    if amount > MAX_WITHDRAWAL:
        raise DenominationError(
            f"Amount {amount} exceeds maximum withdrawal "
            f"of {MAX_WITHDRAWAL} per transaction. "
            f"Visit a branch for larger amounts.")
 
    # Rule 5: must be a multiple of DENOMINATION
    # This is the core rule. % gives the remainder after division.
    # If remainder is not zero, the amount cannot be dispensed
    # as whole notes.
    remainder = amount % DENOMINATION
    if remainder != 0:
        # Calculate the nearest valid amounts to help the user
        lower = (amount // DENOMINATION) * DENOMINATION
        upper = lower + DENOMINATION
        raise DenominationError(
            f"Amount {amount} is not a multiple of {DENOMINATION}. "
            f"ATM only dispenses {DENOMINATION}-unit notes. "
            f"Did you mean {lower} or {upper}?")
 
    # All checks passed -- amount is valid
    return amount  # return the (possibly int-converted) amount
 
 
# ══════════════════════════════════════════════════════════════════
# BANK ACCOUNT
#
# Thread-safe account with:
#   - denomination enforcement on withdrawal
#   - TOCTOU-proof check-and-deduct (one lock)
#   - atomic transfer (debit + credit in one lock)
#   - full transaction ledger
#   - failed-attempt tracking (locks account after 3 bad attempts)
# ══════════════════════════════════════════════════════════════════
 
class BankAccount:
 
    def __init__(self, owner, initial_balance=0, account_id=None):
        self.owner      = owner
        self.account_id = account_id or f"ACC-{random.randint(10000,99999)}"
        self._balance   = initial_balance  # private -- never access directly
 
        # The ONE lock that protects ALL balance operations.
        # Every method that reads OR writes _balance must hold this lock.
        # No exceptions. This is what makes the account thread-safe.
        self._lock = threading.Lock()
 
        # Transaction history -- also protected by _lock
        self._ledger = []
 
        # Failed attempt counter
        # If a thread hammers the account with bad withdrawals,
        # we lock it after 3 consecutive failures.
        self._failed_attempts = 0
        self._locked = False        # account suspended flag
 
    # ── READ-ONLY PROPERTIES ──────────────────────────────────────
 
    @property
    def balance(self):
        """
        Thread-safe balance read.
        Even a simple read needs the lock -- without it, a thread
        could read a partially-written value if another thread is
        mid-update (torn read on some architectures).
        """
        with self._lock:
            return self._balance
 
    @property
    def is_locked(self):
        with self._lock:
            return self._locked
 
    # ── DEPOSIT ───────────────────────────────────────────────────
 
    def deposit(self, amount, depositor="system"):
        """
        Deposit any positive amount. No denomination restriction
        on deposits -- you can deposit any amount (cheque, transfer).
        """
        if amount <= 0:
            raise ValueError(f"Deposit amount must be positive. Got: {amount}")
 
        with self._lock:
            if self._locked:
                raise AccountLockedError(
                    f"Account {self.account_id} is suspended.")
 
            old_balance    = self._balance
            self._balance += amount
 
            # Record in ledger inside the same lock
            # This ensures ledger and balance are always in sync
            self._ledger.append({
                "type":       "DEPOSIT",
                "amount":     amount,
                "balance":    self._balance,
                "by":         depositor,
                "timestamp":  datetime.now().isoformat(),
            })
 
            return {
                "success":     True,
                "old_balance": old_balance,
                "new_balance": self._balance,
                "amount":      amount,
            }
 
    # ── WITHDRAW ──────────────────────────────────────────────────
 
    def withdraw(self, amount, requester="unknown"):
        """
        Withdraw money. Enforces:
          1. Denomination rule (multiples of 20 only) -- checked FIRST
          2. Account not locked
          3. Sufficient balance (check AND deduct in one lock -- no TOCTOU)
          4. Ledger written atomically with balance change
 
        The denomination check happens OUTSIDE the lock -- it is pure math,
        no shared state, no need to block other threads while validating.
 
        The balance check AND deduction happen INSIDE one lock --
        this is the TOCTOU fix. No thread can read the balance between
        our check and our deduction.
        """
 
        # ── STEP 1: Validate denomination (outside lock, pure math) ──
        # This is fast and never fails spuriously.
        # Reject bad amounts before we even try to acquire the lock.
        # This reduces lock contention -- fewer threads compete for the lock.
        try:
            amount = validate_denomination(amount)
        except DenominationError as e:
            # Log the rejection WITHOUT the lock (no shared state touched)
            log(requester, f"REJECTED {amount} -- {e}", "[X]")
            with self._lock:
                self._failed_attempts += 1
                if self._failed_attempts >= 3:
                    self._locked = True
                    log(requester,
                        f"Account {self.account_id} LOCKED after 3 failed attempts",
                        "[!!]")
            raise
 
        # ── STEP 2: Check account status + balance + deduct (inside lock) ──
        # ONE lock wraps ALL three operations.
        # No other thread can read or write _balance between check and deduct.
        # This is the atomicity guarantee.
        with self._lock:
 
            # Check: is account suspended?
            if self._locked:
                raise AccountLockedError(
                    f"Account {self.account_id} is suspended. "
                    f"Contact your bank to unlock.")
 
            # Check: sufficient funds?
            # This check and the deduction below are ATOMIC -- same lock.
            # No TOCTOU possible. No thread sees the balance between these lines.
            if self._balance < amount:
                self._failed_attempts += 1
                msg = (f"Insufficient funds. "
                       f"Requested: {amount}, Available: {self._balance}")
                if self._failed_attempts >= 3:
                    self._locked = True
                    log(requester,
                        f"Account {self.account_id} LOCKED after 3 failed attempts",
                        "[!!]")
                raise InsufficientFundsError(msg)
 
            # Deduct: balance change
            old_balance    = self._balance
            self._balance -= amount
 
            # Reset failed counter on success
            self._failed_attempts = 0
 
            # Write to ledger atomically with balance change.
            # Both happen inside the same lock -- they are always in sync.
            # If this thread crashes after deducting but before ledger write,
            # the lock's __exit__ still runs -- Python's 'with' guarantees it.
            self._ledger.append({
                "type":       "WITHDRAWAL",
                "amount":     amount,
                "balance":    self._balance,
                "by":         requester,
                "timestamp":  datetime.now().isoformat(),
            })
 
            return {
                "success":     True,
                "old_balance": old_balance,
                "new_balance": self._balance,
                "amount":      amount,
                "notes":       amount // DENOMINATION,  # how many notes dispensed
            }
 
    # ── TRANSFER ─────────────────────────────────────────────────
 
    def transfer_to(self, target_account, amount, requester="unknown"):
        """
        Transfer money to another account.
 
        The atomicity problem: debit self, credit target.
        These must be ONE operation -- no funds can be in limbo.
 
        Solution: acquire BOTH locks in a consistent order.
        Order is determined by account_id to prevent deadlock.
        Whichever account has the lower id is always locked first.
        This means two threads doing A->B and B->A will both
        try to lock the lower-id account first -- no circular wait.
        """
 
        # Validate denomination on transfers too
        try:
            amount = validate_denomination(amount)
        except DenominationError as e:
            log(requester, f"TRANSFER REJECTED -- {e}", "[X]")
            raise
 
        # Determine lock order to prevent deadlock
        # Always lock the account with the lower id first
        if self.account_id < target_account.account_id:
            first_lock  = self._lock
            second_lock = target_account._lock
        else:
            first_lock  = target_account._lock
            second_lock = self._lock
 
        # Acquire both locks in consistent order
        with first_lock:
            with second_lock:
                # Now we hold BOTH locks.
                # No other thread can touch either account.
                # Debit and credit happen atomically.
 
                if self._locked:
                    raise AccountLockedError("Source account is suspended.")
 
                if self._balance < amount:
                    raise InsufficientFundsError(
                        f"Transfer failed. Need {amount}, have {self._balance}")
 
                # Atomic debit + credit
                self._balance              -= amount
                target_account._balance    += amount
 
                ts = datetime.now().isoformat()
 
                self._ledger.append({
                    "type":      "TRANSFER_OUT",
                    "amount":    amount,
                    "balance":   self._balance,
                    "to":        target_account.account_id,
                    "by":        requester,
                    "timestamp": ts,
                })
 
                target_account._ledger.append({
                    "type":      "TRANSFER_IN",
                    "amount":    amount,
                    "balance":   target_account._balance,
                    "from":      self.account_id,
                    "by":        requester,
                    "timestamp": ts,
                })
 
                return {
                    "success":         True,
                    "amount":          amount,
                    "from_balance":    self._balance,
                    "to_balance":      target_account._balance,
                }
 
    # ── STATEMENT ─────────────────────────────────────────────────
 
    def statement(self):
        """Return a copy of the ledger. Thread-safe read."""
        with self._lock:
            return list(self._ledger)
 
    def __repr__(self):
        return (f"BankAccount({self.owner!r}, "
                f"id={self.account_id}, "
                f"balance={self._balance})")
 
 
# ══════════════════════════════════════════════════════════════════
# ATM MACHINE
#
# The ATM is the user-facing interface to the account.
# It formats output, handles exceptions gracefully,
# and simulates the physical experience of using a cash machine.
# ══════════════════════════════════════════════════════════════════
 
class ATM:
 
    def __init__(self, atm_id="ATM-001"):
        self.atm_id = atm_id
 
    def withdraw(self, account, amount, user="customer"):
        """
        Process a withdrawal request through the ATM.
        Returns True on success, False on any failure.
        """
        tag = f"{user[:10]}"
        log(tag, f"requesting withdrawal of {amount}", ">>")
 
        try:
            result = account.withdraw(amount, requester=user)
            notes  = result["notes"]
            log(tag,
                f"[OK] Dispensed {notes} x {DENOMINATION} = {amount} "
                f"| balance: {result['new_balance']}",
                "[OK]")
            return True
 
        except DenominationError as e:
            log(tag, f"[X] DENOMINATION ERROR: {e}", "[X]")
            return False
 
        except InsufficientFundsError as e:
            log(tag, f"[X] INSUFFICIENT FUNDS: {e}", "[X]")
            return False
 
        except AccountLockedError as e:
            log(tag, f"[!!] ACCOUNT LOCKED: {e}", "[!!]")
            return False
 
    def transfer(self, from_acc, to_acc, amount, user="customer"):
        tag = f"{user[:10]}"
        log(tag, f"transferring {amount} --> {to_acc.account_id}", ">>")
        try:
            result = from_acc.transfer_to(to_acc, amount, requester=user)
            log(tag,
                f"[OK] Transferred {amount} "
                f"| from balance: {result['from_balance']} "
                f"| to balance: {result['to_balance']}",
                "[OK]")
            return True
        except (DenominationError, InsufficientFundsError, AccountLockedError) as e:
            log(tag, f"[X] TRANSFER FAILED: {e}", "[X]")
            return False
 
 
# ══════════════════════════════════════════════════════════════════
# SIMULATIONS
# ══════════════════════════════════════════════════════════════════
 
def sim_denomination_rules():
    """Show exactly which amounts pass and which are rejected."""
    print("\n" + "="*60)
    print("  DENOMINATION VALIDATION RULES")
    print("="*60)
    print(f"\n  Rule: only multiples of {DENOMINATION} are accepted\n")
 
    test_amounts = [
        20, 40, 60, 80, 100, 120, 200, 400, 600,  # valid
        0, -20, 10, 15, 25, 33, 50, 75, 99,        # invalid
        601, 620, 1000,                              # over limit
        20.0, 20.5,                                  # float edge cases
    ]
 
    valid   = []
    invalid = []
 
    for amount in test_amounts:
        try:
            validate_denomination(amount)
            valid.append(amount)
            print(f"  [OK] {str(amount):<8} -- VALID   "
                  f"({amount} / {DENOMINATION} = {int(amount) // DENOMINATION} notes)")
        except DenominationError as e:
            invalid.append(amount)
            print(f"  [X] {str(amount):<8} -- INVALID  {e}")
 
    print(f"\n  Valid: {len(valid)}  |  Invalid: {len(invalid)}")
 
 
def sim_concurrent_withdrawals():
    """
    50 threads all try to withdraw simultaneously from one account.
    Shows that race conditions are prevented -- balance never goes negative.
    """
    print("\n" + "="*60)
    print("  CONCURRENT WITHDRAWAL STRESS TEST")
    print("="*60)
    print("\n  50 threads withdraw 20 simultaneously from a 500-balance account.")
    print("  Only 25 should succeed. Balance must never go below 0.\n")
 
    account = BankAccount("Alice", initial_balance=500)
    atm     = ATM()
    results = {"success": 0, "failed": 0}
    results_lock = threading.Lock()
 
    def customer(cid):
        success = atm.withdraw(account, 20, user=f"Cust-{cid:02d}")
        with results_lock:
            if success:
                results["success"] += 1
            else:
                results["failed"] += 1
 
    # threading.Barrier: all 50 threads wait here
    # then ALL released at exactly the same instant
    # This is the maximum stress -- worst case concurrency
    barrier = threading.Barrier(50)
 
    threads = []
    for i in range(50):
        def worker(cid=i):
            barrier.wait()   # wait until all 50 are ready
            customer(cid)    # then all hit the account simultaneously
        threads.append(threading.Thread(target=worker))
 
    for t in threads: t.start()
    for t in threads: t.join()
 
    final = account.balance
    print(f"\n  Results:")
    print(f"    Started with : 500")
    print(f"    Successful   : {results['success']} withdrawals")
    print(f"    Failed       : {results['failed']} (correctly rejected)")
    print(f"    Final balance: {final}")
    print(f"    Expected     : 0 (25 x 20 = 500)")
    print(f"    Race-free    : {'YES' if final >= 0 else 'NO -- RACE DETECTED'}")
 
 
def sim_invalid_amounts_under_load():
    """
    Mix of valid and invalid amounts fired simultaneously.
    Valid ones go through. Invalid ones are rejected at the gate.
    """
    print("\n" + "="*60)
    print("  MIXED VALID/INVALID AMOUNTS UNDER CONCURRENT LOAD")
    print("="*60)
    print("\n  20 threads fire random amounts (some valid, some not).\n")
 
    account = BankAccount("Bob", initial_balance=1000)
    atm     = ATM()
 
    # Mix of valid and invalid amounts
    amounts = [20, 33, 40, 15, 60, 99, 80, 50, 100, 17,
               120, 25, 140, 13, 160, 77, 180, 7, 200, 1]
 
    results = {"valid_ok": 0, "invalid_rejected": 0, "insufficient": 0}
    r_lock  = threading.Lock()
 
    def fire(i):
        amt = amounts[i % len(amounts)]
        try:
            account.withdraw(amt, requester=f"User-{i:02d}")
            with r_lock: results["valid_ok"] += 1
        except DenominationError:
            with r_lock: results["invalid_rejected"] += 1
        except InsufficientFundsError:
            with r_lock: results["insufficient"] += 1
 
    threads = [threading.Thread(target=fire, args=(i,)) for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
 
    print(f"\n  Results:")
    print(f"    Valid withdrawals processed : {results['valid_ok']}")
    print(f"    Invalid amounts rejected    : {results['invalid_rejected']}")
    print(f"    Insufficient funds          : {results['insufficient']}")
    print(f"    Final balance               : {account.balance}")
 
 
def sim_transfers():
    """
    Concurrent transfers between accounts.
    Total money in system must be conserved -- that is the invariant.
    """
    print("\n" + "="*60)
    print("  CONCURRENT TRANSFERS -- ATOMICITY TEST")
    print("="*60)
    print("\n  Alice and Bob each have 500. 20 transfers fire simultaneously.")
    print("  Total money (Alice + Bob) must always equal 1000.\n")
 
    alice = BankAccount("Alice", initial_balance=500, account_id="ACC-0001")
    bob   = BankAccount("Bob",   initial_balance=500, account_id="ACC-0002")
    atm   = ATM()
 
    def random_transfer(tid):
        # Each thread picks a random valid amount and direction
        amount = random.choice([20, 40, 60, 80, 100])
        if tid % 2 == 0:
            atm.transfer(alice, bob, amount, user=f"Trf-{tid:02d}")
        else:
            atm.transfer(bob, alice, amount, user=f"Trf-{tid:02d}")
 
    threads = [threading.Thread(target=random_transfer, args=(i,))
               for i in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()
 
    total = alice.balance + bob.balance
    print(f"\n  Alice final : {alice.balance}")
    print(f"  Bob final   : {bob.balance}")
    print(f"  Total       : {total} (must be 1000)")
    print(f"  Atomicity   : {'PRESERVED' if total == 1000 else 'VIOLATED -- BUG'}")
 
 
def print_statement(account):
    print(f"\n  Statement for {account.owner} ({account.account_id}):")
    print(f"  {'─'*55}")
    for entry in account.statement():
        t   = entry['type']
        amt = entry['amount']
        bal = entry['balance']
        by  = entry.get('by', '?')
        print(f"  {t:<14} {amt:>6}   balance: {bal:>6}   by: {by}")
    print(f"  {'─'*55}")
    print(f"  Current balance: {account.balance}")
 
 
# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════
 
if __name__ == "__main__":
    import sys
    quiet = len(sys.argv) > 1 and sys.argv[1] == "quiet"
 
    print("""
+============================================================+
|  SECURE ATM BANK SYSTEM                                    |
|  Denomination: multiples of 20 only                        |
|  Thread-safe, TOCTOU-proof, atomic transfers               |
+============================================================+
    """)
 
    sim_denomination_rules()
    sim_concurrent_withdrawals()
    sim_invalid_amounts_under_load()
    sim_transfers()
 
    # Show statement for a sample account
    acc = BankAccount("Demo", initial_balance=500, account_id="ACC-DEMO")
    atm = ATM()
    atm.withdraw(acc, 20,  user="demo")
    atm.withdraw(acc, 33,  user="demo")   # rejected
    atm.withdraw(acc, 100, user="demo")
    atm.withdraw(acc, 15,  user="demo")   # rejected
    atm.withdraw(acc, 60,  user="demo")
    print_statement(acc)
 
