"""
RACE CONDITION DETECTION SUITE
================================
Four detection methods, each demonstrated with:
  1. A piece of code that HAS a race condition
  2. The detection method catching it
  3. What the output tells you
  4. How to interpret the result

Methods covered:
  A. Static Analysis   -- scan code structure for shared state without locks
  B. Stress / Fuzzing  -- hammer with parallelism until the bug surfaces
  C. Code Review Bot   -- automated pattern matching like a senior reviewer
  D. Timestamp Logging -- instrument code to catch interleavings in production
"""

import threading
import time
import random
import inspect
import re
import ast
import sys
import io
from datetime import datetime
from collections import defaultdict
from contextlib import contextmanager

# ── print helpers ────────────────────────────────────────────────────
_pl = threading.Lock()

def log(tag, msg, sym="-"):
    with _pl:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"  {ts}  [{tag:<18}] {sym} {msg}")

def section(t):
    print(f"\n{'='*70}\n  {t}\n{'='*70}")

def divider(t=""):
    pad = f"  {'-'*28} {t} {'-'*28}" if t else f"  {'-'*68}"
    print(pad)

def ok(msg):
    with _pl: print(f"  [PASS] {msg}")

def fail(msg):
    with _pl: print(f"  [FAIL] {msg}")

def info(msg):
    with _pl: print(f"  [INFO] {msg}")


# ════════════════════════════════════════════════════════════════════
# THE SHARED BUGGY SYSTEMS WE WILL TEST
#
# These are realistic code snippets that contain race conditions.
# Each detection method will be applied to catch the bug.
# ════════════════════════════════════════════════════════════════════

class BuggyBankAccount:
    """
    A bank account with a race condition in withdraw().

    The bug: balance is checked and modified without a lock.
    check_balance() and deduct() are two separate operations
    with no protection between them -- classic TOCTOU.
    """
    def __init__(self, initial=1000):
        self.balance = initial          # shared mutable state -- no lock
        self._lock   = threading.Lock() # lock exists but is NOT used in withdraw()

    def deposit(self, amount):
        # deposit IS correct -- uses the lock
        with self._lock:
            self.balance += amount

    def withdraw(self, amount, name="?"):
        # BUG: check and deduct are separated -- no lock wraps both
        if self.balance >= amount:     # CHECK  (unprotected read)
            time.sleep(0.001)          # gap -- simulates DB latency
            self.balance -= amount     # DEDUCT (unprotected write)
            return True
        return False

    def safe_withdraw(self, amount, name="?"):
        # CORRECT: lock wraps the entire check-and-deduct
        with self._lock:
            if self.balance >= amount:
                self.balance -= amount
                return True
            return False


class BuggyCounter:
    """
    A shared counter incremented by multiple threads.

    The bug: += is not atomic. It is:
      1. READ  current value
      2. ADD   1
      3. WRITE result back
    Threads can interleave between any of these steps.
    """
    def __init__(self):
        self.count = 0             # shared -- no protection

    def increment(self):
        self.count += 1            # BUG: 3 operations, not 1

    def safe_increment(self):
        # atomic increment using a lock
        pass  # shown in fix section


class BuggyCache:
    """
    A lazy-initialised cache.

    The bug: the None check and initialisation are not locked together.
    Multiple threads see None and all initialise -- Lazy Init Race.
    """
    def __init__(self):
        self._cache = None
        self._lock  = threading.Lock()
        self.init_count = 0

    def get_data(self):
        if self._cache is None:            # BUG: unprotected check
            time.sleep(0.05)               # expensive initialisation
            self._cache = {"key": "value"}
            self.init_count += 1           # how many times did we init?
        return self._cache

    def safe_get_data(self):
        if self._cache is None:
            with self._lock:
                if self._cache is None:    # recheck inside lock
                    time.sleep(0.05)
                    self._cache = {"key": "value"}
                    self.init_count += 1
        return self._cache


# ════════════════════════════════════════════════════════════════════
# METHOD A: STATIC ANALYSIS
#
# What it is:
#   Read the code WITHOUT running it. Look for patterns that
#   are structurally dangerous:
#     - shared mutable state (class attributes, globals)
#     - reads/writes to that state outside a lock context
#     - lock objects that exist but are never used in certain methods
#
# How it works:
#   We parse the Python source using the `ast` module -- the same
#   Abstract Syntax Tree that Python itself builds before executing.
#   We walk the tree looking for:
#     1. Every assignment to self.X  (potential shared write)
#     2. Every read of self.X        (potential shared read)
#     3. Whether those accesses are inside a `with lock:` block
#
# Limitations:
#   Static analysis cannot know what runs at runtime.
#   It produces false positives (flags safe code) and
#   false negatives (misses dynamic patterns).
#   But it is FAST -- runs in milliseconds, catches obvious bugs.
# ════════════════════════════════════════════════════════════════════

class StaticRaceAnalyzer(ast.NodeVisitor):
    """
    Walks a Python AST looking for unprotected shared state access.

    Key concepts:
      ast.NodeVisitor -- a class that walks every node in the AST.
        Override visit_X methods to intercept specific node types.

      ast.Assign      -- assignment statement: x = value
      ast.Attribute   -- attribute access: self.balance
      ast.With        -- with statement: with lock:
      ast.FunctionDef -- function definition: def foo(self):
    """

    def __init__(self):
        self.issues        = []    # list of detected problems
        self.in_with_lock  = False # are we currently inside a 'with lock' block?
        self.current_func  = None  # which function are we analysing?
        self.shared_attrs  = set() # attributes written in ANY method (potential shared state)
        self.protected     = set() # (attr, func) pairs accessed inside a lock

    def visit_FunctionDef(self, node):
        """Called for every function definition."""
        prev_func = self.current_func
        self.current_func = node.name

        # First pass: collect all attribute assignments in this function
        for child in ast.walk(node):
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if (isinstance(target, ast.Attribute) and
                            isinstance(target.value, ast.Name) and
                            target.value.id == 'self'):
                        self.shared_attrs.add(target.attr)

        # Second pass: visit children (will trigger other visit_* methods)
        self.generic_visit(node)
        self.current_func = prev_func

    def visit_With(self, node):
        """
        Called for every 'with' statement.
        We check if it looks like a lock context manager:
          with self._lock:
          with lock:
          with some_mutex:
        """
        # Check if any context manager looks like a lock
        is_lock_context = False
        for item in node.items:
            expr = item.context_expr
            # matches: with self._lock, with self.lock, with lock, etc.
            if isinstance(expr, ast.Attribute):
                name = expr.attr.lower()
                if any(kw in name for kw in ['lock', 'mutex', 'semaphore', 'rlock']):
                    is_lock_context = True
            elif isinstance(expr, ast.Name):
                name = expr.id.lower()
                if any(kw in name for kw in ['lock', 'mutex', 'semaphore', 'rlock']):
                    is_lock_context = True

        prev = self.in_with_lock
        if is_lock_context:
            self.in_with_lock = True

        self.generic_visit(node)
        self.in_with_lock = prev

    def visit_Attribute(self, node):
        """
        Called for every attribute access (self.balance, self.count, etc.)
        We check if this is a read/write of a shared attribute outside a lock.
        """
        if (isinstance(node.value, ast.Name) and
                node.value.id == 'self' and
                self.current_func is not None):

            attr = node.attr
            # Skip private/dunder and lock-like attributes
            if (not attr.startswith('__') and
                    not any(kw in attr.lower() for kw in
                            ['lock', 'mutex', 'semaphore', 'rlock', 'event'])):

                if self.in_with_lock:
                    self.protected.add((attr, self.current_func))
                else:
                    # This attribute is accessed outside a lock
                    # Check if it's also accessed inside a lock elsewhere
                    # (meaning it IS shared state, just not protected here)
                    self.issues.append({
                        "attr": attr,
                        "func": self.current_func,
                        "line": getattr(node, 'lineno', '?'),
                        "protected": False,
                    })

        self.generic_visit(node)

    def report(self):
        """
        Cross-reference: if an attribute is protected in one method
        but unprotected in another -- that is a race condition.
        """
        protected_attrs = {attr for attr, _ in self.protected}
        races = []
        seen  = set()

        for issue in self.issues:
            attr = issue["attr"]
            func = issue["func"]
            key  = (attr, func)

            if key in seen:
                continue
            seen.add(key)

            # Only flag if this attr IS protected somewhere
            # (meaning it is genuinely shared state)
            if attr in protected_attrs and attr in self.shared_attrs:
                races.append(issue)

        return races


def run_static_analysis():
    section("METHOD A: STATIC ANALYSIS")
    print("""
  What it does:
    Reads your source code as a syntax tree (AST).
    Looks for shared attributes that are sometimes protected by a lock
    and sometimes not -- that inconsistency is the race condition.

  No code is executed. No threads are started.
  Runs in milliseconds. Catches obvious structural bugs.
    """)

    divider("Analysing BuggyBankAccount")

    # Get source of the class and parse it
    source = inspect.getsource(BuggyBankAccount)
    tree   = ast.parse(source)

    analyzer = StaticRaceAnalyzer()
    analyzer.visit(tree)
    races = analyzer.report()

    if races:
        fail(f"Found {len(races)} potential race condition(s):")
        seen = set()
        for r in races:
            key = (r['attr'], r['func'])
            if key not in seen:
                seen.add(key)
                print(f"     Attribute  : self.{r['attr']}")
                print(f"     In method  : {r['func']}()")
                print(f"     Protected  : {r['protected']} (lock exists but not used here)")
                print(f"     Verdict    : self.{r['attr']} is guarded in other methods")
                print(f"                  but accessed WITHOUT a lock in {r['func']}()")
                print()
    else:
        ok("No races detected (may have false negatives)")

    divider("What the AST sees")
    print("""
  The AST breaks your source into nodes:

  withdraw(self, amount):
    If (self.balance >= amount):     <-- Attribute read, NOT in With(lock)
      time.sleep(0.001)
      self.balance -= amount         <-- Attribute write, NOT in With(lock)

  deposit(self, amount):
    With(self._lock):                <-- With node, contains lock
      self.balance += amount         <-- Attribute write, IS in With(lock)

  Static analyser sees:
    balance is written inside With(lock) in deposit()    --> protected
    balance is written OUTSIDE With(lock) in withdraw()  --> UNPROTECTED
    --> FLAG: inconsistent protection on self.balance
    """)

    divider("Limitations")
    print("""
  False positives: may flag thread-local state as shared
  False negatives: misses races via function calls, closures, globals
  Cannot detect: timing-dependent bugs, lock ordering issues (deadlock)

  Best used as: a first pass before running dynamic tools
    """)


# ════════════════════════════════════════════════════════════════════
# METHOD B: STRESS TESTING / FUZZING WITH CONCURRENCY
#
# What it is:
#   Run the suspicious code with MAXIMUM concurrency and MAXIMUM
#   iterations. If a race condition exists, high parallelism
#   increases the probability that threads interleave at exactly
#   the wrong moment -- making the rare bug frequent.
#
# How it works:
#   1. Identify invariants -- conditions that must ALWAYS be true
#      e.g. "balance must never go below 0"
#           "counter must equal N after N increments"
#           "cache must be initialised exactly once"
#   2. Run N threads, each doing M operations simultaneously
#   3. Check invariants after each run
#   4. Repeat many times -- each run is a different interleaving
#
# The key insight:
#   A race condition creates NON-DETERMINISM.
#   If you run the same code 100 times and get different results,
#   you have found a race condition.
#   Deterministic correct code always produces the same output.
# ════════════════════════════════════════════════════════════════════

class StressTester:
    """
    Runs a function with high concurrency and checks invariants.

    Parameters:
      num_threads  -- how many threads to launch simultaneously
      iterations   -- how many times each thread calls the function
      num_runs     -- how many times to repeat the whole experiment
                      (each run is a different thread interleaving)
    """

    def __init__(self, num_threads=20, iterations=1000, num_runs=5):
        self.num_threads = num_threads
        self.iterations  = iterations
        self.num_runs    = num_runs
        self.results     = []

    def run(self, setup_fn, work_fn, invariant_fn, label="test"):
        """
        setup_fn()      -- creates the object under test, returns it
        work_fn(obj)    -- one unit of work (called N*M times total)
        invariant_fn(obj) -- returns (passed: bool, message: str)
        """
        print(f"\n  Stress test: {label}")
        print(f"  Config: {self.num_threads} threads x "
              f"{self.iterations} iterations x "
              f"{self.num_runs} runs")
        print(f"  Total operations: "
              f"{self.num_threads * self.iterations * self.num_runs:,}\n")

        failures = 0

        for run_idx in range(self.num_runs):
            obj = setup_fn()  # fresh object for each run

            # Create all threads -- they all call work_fn(obj)
            threads = [
                threading.Thread(
                    target=self._worker,
                    args=(obj, work_fn, self.iterations)
                )
                for _ in range(self.num_threads)
            ]

            # Start ALL threads as close together as possible
            # This maximises the chance of interleaving
            barrier = threading.Barrier(self.num_threads)  # synchronise start

            def worker_with_barrier(o, fn, n, b):
                b.wait()  # all threads wait here until all are ready
                for _ in range(n):
                    fn(o)

            threads = [
                threading.Thread(
                    target=worker_with_barrier,
                    args=(obj, work_fn, self.iterations, barrier)
                )
                for _ in range(self.num_threads)
            ]

            for t in threads: t.start()
            for t in threads: t.join()

            # Check invariant
            passed, message = invariant_fn(obj)
            status = "[PASS]" if passed else "[FAIL]"
            if not passed:
                failures += 1

            log(f"Run {run_idx+1}/{self.num_runs}",
                f"{status} {message}", "[OK]" if passed else "[!!]")

        print()
        if failures > 0:
            fail(f"Race condition CONFIRMED: {failures}/{self.num_runs} runs failed")
            info("Non-deterministic results = race condition present")
        else:
            ok(f"All {self.num_runs} runs passed -- race may be rare or absent")

        return failures > 0

    @staticmethod
    def _worker(obj, fn, n):
        for _ in range(n): fn(obj)


def run_stress_testing():
    section("METHOD B: STRESS TESTING / CONCURRENCY FUZZING")
    print("""
  What it does:
    Launches many threads simultaneously, hammers shared state,
    and checks that invariants (correctness conditions) hold.

  Key insight: race conditions are non-deterministic.
    If the same code gives different results across runs -- race found.
    Deterministic correct code always gives the same answer.
    """)

    tester = StressTester(num_threads=30, iterations=500, num_runs=6)

    # ── Test 1: BuggyCounter ──────────────────────────────────────
    divider("Test 1: BuggyCounter -- counter += 1 is not atomic")
    print("""
  Invariant: after 30 threads x 500 increments each,
             counter must equal exactly 15,000.
  If it is less, increments were lost to the race.
    """)

    class BuggyCounterObj:
        def __init__(self): self.count = 0
        def increment(self): self.count += 1  # BUG: not atomic

    def counter_invariant(obj):
        expected = 30 * 500  # 15,000
        actual   = obj.count
        diff     = expected - actual
        msg = (f"count={actual:,} expected={expected:,} "
               f"LOST={diff:,} increments")
        return actual == expected, msg

    tester.run(
        setup_fn    = BuggyCounterObj,
        work_fn     = lambda obj: obj.increment(),
        invariant_fn= counter_invariant,
        label       = "BuggyCounter.increment()"
    )

    # ── Test 2: BuggyBankAccount ──────────────────────────────────
    divider("Test 2: BuggyBankAccount -- TOCTOU in withdraw()")
    print("""
  Invariant: after all withdrawals, balance must be >= 0.
  If balance goes negative, multiple threads withdrew when they shouldn't.
  Initial balance: 1000. Each thread tries to withdraw 10.
    """)

    def bank_work(obj):
        obj.withdraw(10)  # buggy version

    def bank_invariant(obj):
        bal = obj.balance
        msg = f"balance={bal} (must be >= 0)"
        return bal >= 0, msg

    tester.run(
        setup_fn    = lambda: BuggyBankAccount(1000),
        work_fn     = bank_work,
        invariant_fn= bank_invariant,
        label       = "BuggyBankAccount.withdraw() -- TOCTOU"
    )

    # ── Test 3: BuggyCache ────────────────────────────────────────
    divider("Test 3: BuggyCache -- lazy init race")
    print("""
  Invariant: cache must be initialised exactly ONCE.
  If init_count > 1, multiple threads initialised simultaneously.
    """)

    def cache_invariant(obj):
        n   = obj.init_count
        msg = f"init_count={n} (must be exactly 1)"
        return n == 1, msg

    tester_single = StressTester(num_threads=20, iterations=1, num_runs=5)
    tester_single.run(
        setup_fn    = BuggyCache,
        work_fn     = lambda obj: obj.get_data(),
        invariant_fn= cache_invariant,
        label       = "BuggyCache.get_data() -- lazy init"
    )

    divider("Interpretation")
    print("""
  PASS every run   --> race is rare (increase threads/iterations) or absent
  FAIL some runs   --> race confirmed (non-deterministic)
  FAIL every run   --> race is very common / severe

  The threading.Barrier trick:
    All threads call barrier.wait() before starting work.
    The barrier releases ALL of them at exactly the same instant.
    This maximises overlap and makes rare races frequent.

  Rule of thumb for confidence:
    10 threads x 10,000 iterations x 20 runs
    If all pass --> likely safe (not guaranteed -- races can be extremely rare)
    """)


# ════════════════════════════════════════════════════════════════════
# METHOD C: AUTOMATED CODE REVIEW
#
# What it is:
#   Pattern matching on source code -- like having a senior engineer
#   review every method looking for known dangerous patterns.
#
# Patterns we look for:
#   1. Shared state accessed without lock     (TOCTOU / atomicity)
#   2. Multiple lock acquisitions in one fn   (potential deadlock)
#   3. time.sleep() inside a lock             (holds lock too long)
#   4. Global variables in threaded code      (unprotected shared state)
#   5. Mutable default arguments              (shared across all calls)
#   6. Thread created but not joined          (potential resource leak)
#   7. Lock exists but never used in method   (lock forgotten)
# ════════════════════════════════════════════════════════════════════

class CodeReviewBot:
    """
    Automated code reviewer for concurrency issues.

    Uses both regex (fast, simple patterns) and AST (structural patterns).
    Each check returns a list of findings with line numbers and explanations.
    """

    def __init__(self, source_code):
        self.source = source_code
        self.lines  = source_code.split('\n')
        self.tree   = ast.parse(source_code)
        self.findings = []

    def add(self, severity, rule, line, detail):
        self.findings.append({
            "severity": severity,   # HIGH / MEDIUM / LOW
            "rule":     rule,
            "line":     line,
            "detail":   detail,
        })

    # ── CHECK 1: sleep() inside a lock context ───────────────────
    def check_sleep_in_lock(self):
        """
        time.sleep() inside 'with lock:' holds the lock
        for the entire sleep duration. Other threads are blocked
        even though no shared state is being accessed.
        This causes unnecessary contention and can cause starvation.
        """
        in_lock_depth = 0

        for node in ast.walk(self.tree):
            if isinstance(node, ast.With):
                for item in node.items:
                    expr = item.context_expr
                    name = ""
                    if isinstance(expr, ast.Attribute): name = expr.attr
                    elif isinstance(expr, ast.Name):    name = expr.id
                    if any(k in name.lower() for k in ['lock','mutex','semaphore']):
                        # look for sleep calls inside this with block
                        for child in ast.walk(node):
                            if (isinstance(child, ast.Call) and
                                    isinstance(child.func, ast.Attribute) and
                                    child.func.attr == 'sleep'):
                                self.add("MEDIUM", "SLEEP_IN_LOCK",
                                    getattr(child, 'lineno', '?'),
                                    "time.sleep() inside a lock -- holds lock during sleep, "
                                    "blocks other threads unnecessarily. "
                                    "Release lock before sleeping.")

    # ── CHECK 2: global variable used in function ────────────────
    def check_global_mutable_state(self):
        """
        Global variables are shared across all threads by default.
        Any read/write without a lock is a potential race condition.
        """
        # Find all global declarations
        globals_declared = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Global):
                globals_declared.update(node.names)

        if globals_declared:
            for node in ast.walk(self.tree):
                if isinstance(node, ast.Global):
                    for name in node.names:
                        self.add("HIGH", "UNGUARDED_GLOBAL",
                            getattr(node, 'lineno', '?'),
                            f"Global variable '{name}' used in function. "
                            f"Globals are shared across all threads. "
                            f"Protect with a lock or use threading.local().")

    # ── CHECK 3: multiple lock acquisitions (deadlock risk) ──────
    def check_multiple_locks(self):
        """
        A function that acquires more than one lock is a deadlock risk
        if another function acquires them in a different order.
        """
        for func_node in ast.walk(self.tree):
            if not isinstance(func_node, ast.FunctionDef):
                continue

            locks_acquired = []
            for node in ast.walk(func_node):
                if isinstance(node, ast.With):
                    for item in node.items:
                        expr = item.context_expr
                        name = ""
                        if isinstance(expr, ast.Attribute):
                            name = f"{expr.value.id if isinstance(expr.value, ast.Name) else '?'}.{expr.attr}"
                        elif isinstance(expr, ast.Name):
                            name = expr.id
                        if any(k in name.lower() for k in ['lock','mutex','semaphore']):
                            locks_acquired.append((name, getattr(node, 'lineno', '?')))

            if len(locks_acquired) >= 2:
                lock_names = [l[0] for l in locks_acquired]
                self.add("HIGH", "MULTIPLE_LOCKS",
                    locks_acquired[0][1],
                    f"Function '{func_node.name}' acquires {len(locks_acquired)} locks: "
                    f"{lock_names}. "
                    f"If another function acquires them in a different order, "
                    f"deadlock is possible. Ensure consistent lock ordering globally.")

    # ── CHECK 4: thread started but join() not called ────────────
    def check_unjoinable_threads(self):
        """
        A Thread that is started but never joined may outlive the
        function that created it, accessing state that has been
        destroyed. This is also a resource leak.
        """
        for func_node in ast.walk(self.tree):
            if not isinstance(func_node, ast.FunctionDef):
                continue

            started  = []
            joined   = []

            for node in ast.walk(func_node):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        if node.func.attr == 'start':
                            started.append(getattr(node, 'lineno', '?'))
                        elif node.func.attr == 'join':
                            joined.append(getattr(node, 'lineno', '?'))

            if len(started) > len(joined):
                self.add("MEDIUM", "UNJOINED_THREAD",
                    started[0],
                    f"Function '{func_node.name}' starts {len(started)} thread(s) "
                    f"but calls join() only {len(joined)} time(s). "
                    f"Unjoined threads may outlive their context and access freed state.")

    # ── CHECK 5: bare attribute increment (+=) ───────────────────
    def check_unsafe_increment(self):
        """
        x += 1 on a shared attribute is not atomic.
        It compiles to: LOAD x, ADD 1, STORE x -- three operations.
        Threads can interleave between any of them.
        """
        for node in ast.walk(self.tree):
            if isinstance(node, ast.AugAssign):
                target = node.target
                # self.something += ...
                if (isinstance(target, ast.Attribute) and
                        isinstance(target.value, ast.Name) and
                        target.value.id == 'self'):
                    # Check if we are inside a With(lock) block
                    # (simplified: just flag all for review)
                    self.add("HIGH", "NONATOMIC_INCREMENT",
                        getattr(node, 'lineno', '?'),
                        f"self.{target.attr} += ... is NOT atomic. "
                        f"Compile to READ-ADD-WRITE. Threads interleave between steps. "
                        f"Wrap in a lock or use an atomic type.")

    def run_all_checks(self):
        self.check_sleep_in_lock()
        self.check_global_mutable_state()
        self.check_multiple_locks()
        self.check_unjoinable_threads()
        self.check_unsafe_increment()
        return self.findings

    def print_report(self):
        if not self.findings:
            ok("No issues found by code review bot")
            return

        high   = [f for f in self.findings if f['severity'] == 'HIGH']
        medium = [f for f in self.findings if f['severity'] == 'MEDIUM']
        low    = [f for f in self.findings if f['severity'] == 'LOW']

        print(f"\n  CODE REVIEW REPORT")
        print(f"  {'─'*60}")
        print(f"  HIGH:   {len(high)} issue(s)")
        print(f"  MEDIUM: {len(medium)} issue(s)")
        print(f"  LOW:    {len(low)} issue(s)")
        print(f"  {'─'*60}\n")

        for f in self.findings:
            sev_icon = {"HIGH":"[!!]", "MEDIUM":"[!]", "LOW":"[i]"}[f['severity']]
            print(f"  {sev_icon} [{f['severity']:<6}] Rule: {f['rule']}")
            print(f"     Line    : {f['line']}")
            print(f"     Finding : {f['detail']}")
            print()


def run_code_review():
    section("METHOD C: AUTOMATED CODE REVIEW BOT")
    print("""
  What it does:
    Applies a set of named rules to your source code.
    Each rule looks for a known dangerous pattern.
    Returns findings with severity, line number, and explanation.

  Like a senior engineer reviewing every method systematically.
  No code is executed. Produces actionable findings.
    """)

    # Review the buggy systems we defined at the top
    divider("Reviewing BuggyBankAccount")
    source = inspect.getsource(BuggyBankAccount)
    bot = CodeReviewBot(source)
    bot.run_all_checks()
    bot.print_report()

    divider("Reviewing BuggyCounter")
    source = inspect.getsource(BuggyCounter)
    bot2 = CodeReviewBot(source)
    bot2.run_all_checks()
    bot2.print_report()

    divider("Reviewing a Deadlock-prone snippet")
    deadlock_code = '''
class ResourceManager:
    def __init__(self):
        self.lock_a = threading.Lock()
        self.lock_b = threading.Lock()
        self.resource_a = 0
        self.resource_b = 0

    def transfer_a_to_b(self, amount):
        with self.lock_a:
            with self.lock_b:
                self.resource_a -= amount
                self.resource_b += amount

    def transfer_b_to_a(self, amount):
        with self.lock_b:       # different order -- deadlock risk
            with self.lock_a:
                self.resource_b -= amount
                self.resource_a += amount

    def dangerous_operation(self):
        with self.lock_a:
            time.sleep(0.5)     # holds lock while sleeping
            self.resource_a += 1
'''
    bot3 = CodeReviewBot(deadlock_code)
    bot3.run_all_checks()
    bot3.print_report()


# ════════════════════════════════════════════════════════════════════
# METHOD D: TIMESTAMP LOGGING + INTERLEAVING DETECTION
#
# What it is:
#   Instrument your code to log every access to shared state
#   with a precise timestamp and thread ID.
#   Then analyse the log to find overlapping accesses --
#   two threads touching the same state at the same time.
#
# How it works:
#   1. Wrap shared state in a monitored proxy object
#   2. Every read and write is logged: timestamp, thread, operation
#   3. After execution, scan the log for overlapping windows
#      An overlap = thread A writes, thread B reads/writes, A not done yet
#
# This is the PRODUCTION detection method.
#   You cannot always run TSan in production.
#   But you CAN add lightweight logging and analyse it post-hoc.
#   The timestamp log tells you exactly which threads interleaved
#   and at what precise moment.
# ════════════════════════════════════════════════════════════════════

class MonitoredValue:
    """
    A proxy wrapper around a shared value that logs every access.

    Every read and write is recorded with:
      - timestamp (nanosecond precision)
      - thread ID and name
      - operation type (read / write)
      - value before and after

    After a test run, call analyse() to find overlapping accesses.
    """

    def __init__(self, initial_value, name="value"):
        self._value   = initial_value
        self._name    = name
        self._log     = []          # all access records
        self._lock    = threading.Lock()  # protects the log itself
        self._active  = {}          # thread_id -> start_time of current operation

    @property
    def value(self):
        """Logged read."""
        t    = threading.current_thread()
        tid  = t.ident
        tname = t.name
        ts   = time.perf_counter_ns()

        with self._lock:
            self._log.append({
                "op":     "READ",
                "thread": tname,
                "tid":    tid,
                "ts_ns":  ts,
                "value":  self._value,
            })
        return self._value

    @value.setter
    def value(self, new_val):
        """Logged write."""
        t     = threading.current_thread()
        tid   = t.ident
        tname = t.name
        ts    = time.perf_counter_ns()

        with self._lock:
            old = self._value
            self._value = new_val
            self._log.append({
                "op":      "WRITE",
                "thread":  tname,
                "tid":     tid,
                "ts_ns":   ts,
                "old_val": old,
                "new_val": new_val,
            })

    def get_log(self):
        with self._lock:
            return list(self._log)

    def analyse(self):
        """
        Scan the access log for dangerous interleavings.

        A dangerous interleaving is:
          Thread A: READ  value=X  at t=100
          Thread B: WRITE value=Y  at t=105   (B writes between A's read and act)
          Thread A: WRITE value=Z  at t=110   (A overwrites B's write -- lost update)

        We detect this by looking for write-write and read-write pairs
        from different threads that are close in time (within 1ms).
        """
        log    = self.get_log()
        issues = []
        window = 1_000_000  # 1 millisecond in nanoseconds

        for i, entry_a in enumerate(log):
            for entry_b in log[i+1:]:
                # only interested in different threads
                if entry_a['tid'] == entry_b['tid']:
                    continue

                # within the time window
                if entry_b['ts_ns'] - entry_a['ts_ns'] > window:
                    break

                # dangerous patterns:
                # READ then WRITE from different threads
                # WRITE then WRITE from different threads
                if entry_a['op'] == 'READ' and entry_b['op'] == 'WRITE':
                    issues.append({
                        "type":    "READ-WRITE RACE",
                        "thread_a": entry_a['thread'],
                        "thread_b": entry_b['thread'],
                        "ts_a":    entry_a['ts_ns'],
                        "ts_b":    entry_b['ts_ns'],
                        "gap_us":  (entry_b['ts_ns'] - entry_a['ts_ns']) // 1000,
                        "detail":  f"{entry_a['thread']} read {entry_a['value']}, "
                                   f"then {entry_b['thread']} wrote {entry_b['new_val']} "
                                   f"{(entry_b['ts_ns']-entry_a['ts_ns'])//1000}us later",
                    })
                elif entry_a['op'] == 'WRITE' and entry_b['op'] == 'WRITE':
                    issues.append({
                        "type":    "WRITE-WRITE RACE",
                        "thread_a": entry_a['thread'],
                        "thread_b": entry_b['thread'],
                        "ts_a":    entry_a['ts_ns'],
                        "ts_b":    entry_b['ts_ns'],
                        "gap_us":  (entry_b['ts_ns'] - entry_a['ts_ns']) // 1000,
                        "detail":  f"{entry_a['thread']} wrote {entry_a['new_val']}, "
                                   f"then {entry_b['thread']} wrote {entry_b['new_val']} "
                                   f"{(entry_b['ts_ns']-entry_a['ts_ns'])//1000}us later",
                    })

        return issues


class InterleaveLogger:
    """
    Instruments a function with entry/exit timestamps per thread.
    Detects overlapping executions of a critical section.

    Usage:
      logger = InterleaveLogger("withdraw")
      logger.enter()
      ... critical section ...
      logger.exit()

    If two threads are inside the critical section simultaneously,
    their enter/exit windows overlap -- that is a race condition.
    """

    def __init__(self, name):
        self.name    = name
        self._lock   = threading.Lock()
        self._active = {}   # tid -> enter_time
        self._events = []   # all enter/exit events

    def enter(self):
        t  = threading.current_thread()
        ts = time.perf_counter_ns()
        with self._lock:
            self._active[t.ident] = ts
            self._events.append({
                "event":  "ENTER",
                "thread": t.name,
                "tid":    t.ident,
                "ts_ns":  ts,
                "concurrent": len(self._active),  # how many threads inside right now
            })

    def exit(self):
        t  = threading.current_thread()
        ts = time.perf_counter_ns()
        with self._lock:
            enter_ts = self._active.pop(t.ident, ts)
            self._events.append({
                "event":    "EXIT",
                "thread":   t.name,
                "tid":      t.ident,
                "ts_ns":    ts,
                "duration": (ts - enter_ts) // 1000,  # microseconds
                "concurrent": len(self._active),
            })

    def analyse(self):
        """Find moments where concurrency > 1 (overlap = race)."""
        overlaps = [e for e in self._events
                    if e["event"] == "ENTER" and e["concurrent"] > 1]
        return overlaps, self._events


def run_timestamp_logging():
    section("METHOD D: TIMESTAMP LOGGING + INTERLEAVING DETECTION")
    print("""
  What it does:
    Wraps shared state in a proxy that logs every read and write
    with nanosecond timestamps and thread IDs.
    Then analyses the log to find overlapping accesses.

  This is the PRODUCTION method -- you can deploy this logging
  in production systems to catch races that only appear under real load.
    """)

    # ── Demo 1: MonitoredValue catches lost updates ───────────────
    divider("Demo 1: MonitoredValue -- detecting lost writes")
    print("""
  We run the buggy counter (value += 1) through 10 threads.
  The MonitoredValue proxy logs every read and write.
  We then look for read-write and write-write races in the log.
    """)

    monitored = MonitoredValue(0, "counter")

    def buggy_increment_monitored():
        v = monitored.value      # READ
        time.sleep(0.0001)       # gap -- makes interleaving more likely
        monitored.value = v + 1  # WRITE (based on potentially stale read)

    threads = [
        threading.Thread(
            target=buggy_increment_monitored,
            name=f"T{i:02d}"
        )
        for i in range(10)
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    final_value = monitored.value
    expected    = 10
    log_entries = monitored.get_log()
    races       = monitored.analyse()

    info(f"Expected final value: {expected}")
    info(f"Actual final value:   {final_value}")
    info(f"Total log entries:    {len(log_entries)}")
    info(f"Races detected:       {len(races)}")
    print()

    if races:
        fail(f"Race condition detected via timestamp analysis:")
        # Show the first 3 races
        for i, race in enumerate(races[:3]):
            print(f"\n  Race #{i+1}: {race['type']}")
            print(f"    {race['detail']}")
            print(f"    Gap between accesses: {race['gap_us']} microseconds")
            print(f"    This gap is where the OS can switch threads")
    else:
        ok("No races detected in this run (increase iterations)")

    # ── Demo 2: InterleaveLogger catches concurrent critical sections
    divider("Demo 2: InterleaveLogger -- catching concurrent critical sections")
    print("""
  We instrument a critical section (the buggy withdraw).
  The logger records when each thread enters and exits.
  If two threads are inside simultaneously -- that is the race.
    """)

    il = InterleaveLogger("withdraw_critical_section")
    account_balance = [500]  # use list for mutability in closure

    def instrumented_buggy_withdraw(amount, name):
        il.enter()                              # log: thread entering
        if account_balance[0] >= amount:        # CHECK (unprotected)
            time.sleep(0.005)                   # gap
            account_balance[0] -= amount        # ACT (unprotected)
        il.exit()                               # log: thread exiting

    withdrawers = [
        threading.Thread(
            target=instrumented_buggy_withdraw,
            args=(500, f"Thread-{i}"),
            name=f"Withdrawer-{i:02d}"
        )
        for i in range(5)
    ]
    for t in withdrawers: t.start()
    for t in withdrawers: t.join()

    overlaps, all_events = il.analyse()

    info(f"Final balance:   {account_balance[0]} (started at 500, should be >= 0)")
    info(f"Total events:    {len(all_events)}")
    info(f"Concurrent overlaps detected: {len(overlaps)}")

    if overlaps:
        print()
        fail("Critical section entered by multiple threads simultaneously:")
        for ov in overlaps[:3]:
            print(f"\n  Thread  : {ov['thread']}")
            print(f"  Entered at: {ov['ts_ns']}")
            print(f"  Other threads inside at same time: {ov['concurrent'] - 1}")
            print(f"  This means: the 'critical section' is NOT atomic")
            print(f"  Fix: add a lock so only 1 thread can be inside at a time")

    divider("Reading the timestamp log")
    print("""
  How to interpret the log output:

  READ-WRITE RACE:
    Thread A read value X at time T1.
    Thread B wrote new value Y at time T1 + gap.
    Thread A will now act on X (stale) -- Y is the real value.
    A's subsequent write will overwrite B's update -- lost update.

  WRITE-WRITE RACE:
    Thread A wrote X at T1.
    Thread B wrote Y at T1 + gap.
    One of these writes is lost -- last writer wins arbitrarily.

  OVERLAP in critical section:
    Two threads are inside the same code block at the same time.
    If that block reads/writes shared state -- it is a race.
    The fix: make the block a critical section with a lock.

  Gap size tells you how hard the race is to trigger:
    < 10 microseconds --> very common race, seen under any load
    10-100 microseconds --> moderate, seen under concurrent load
    > 1 millisecond --> rare, may only appear under heavy production load
    """)


# ════════════════════════════════════════════════════════════════════
# PUTTING IT ALL TOGETHER: THE DETECTION PIPELINE
# ════════════════════════════════════════════════════════════════════

def run_detection_pipeline():
    section("THE COMPLETE DETECTION PIPELINE")
    print("""
  In practice, use all four methods in order:

  STEP 1: STATIC ANALYSIS  (before running anything)
  ────────────────────────────────────────────────────
  Tool    : ast module (Python), pylint, mypy, or PyChecker
  When    : on every commit, in CI pipeline
  Catches : obvious structural bugs in seconds
  Miss    : dynamic patterns, correct-looking but timed wrong code

  STEP 2: CODE REVIEW BOT  (automated + human)
  ────────────────────────────────────────────────────
  Tool    : custom rules (like our CodeReviewBot above)
            + human senior review for logic
  When    : pull request review
  Catches : lock ordering, sleep-in-lock, unjoined threads
  Miss    : bugs that only appear at runtime scale

  STEP 3: STRESS TESTING   (in development / staging)
  ────────────────────────────────────────────────────
  Tool    : threading.Barrier + invariant checks (like our StressTester)
            + ThreadSanitizer (TSan) on C/C++ extensions
            + Helgrind (Valgrind) for native code
  When    : test suite, before every release
  Catches : non-deterministic bugs, confirms races empirically
  Miss    : races that need specific production timing/data

  STEP 4: TIMESTAMP LOGGING  (in production)
  ────────────────────────────────────────────────────
  Tool    : MonitoredValue, InterleaveLogger (like above)
            + distributed tracing (Jaeger, OpenTelemetry)
            + structured logging with thread IDs
  When    : production, under real load
  Catches : races that only appear under real conditions
  Miss    : nothing -- if it happens, the log records it

  CONFIDENCE LEVEL:
  ─────────────────
  Passed all 4 methods --> HIGH confidence, not race-free (races are subtle)
  Failed any method    --> race confirmed or very likely

  THE ONLY GUARANTEE OF RACE-FREE CODE:
  ──────────────────────────────────────
  Formal verification (TLA+, SPIN model checker) can PROVE
  a concurrent system is race-free. Used in:
    - AWS (they use TLA+ to verify DynamoDB, S3)
    - Microsoft (Azure Cosmos DB)
    - Intel (CPU microcode verification)
  For application code: tests + review + logging = practical standard.
    """)


# ════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("""
+======================================================================+
|        RACE CONDITION DETECTION SUITE                               |
|        4 methods -- static, stress, review, logging                 |
+======================================================================+
    """)

    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    if mode in ("all", "static"):
        run_static_analysis()

    if mode in ("all", "stress"):
        run_stress_testing()

    if mode in ("all", "review"):
        run_code_review()

    if mode in ("all", "logging"):
        run_timestamp_logging()

    if mode in ("all", "pipeline"):
        run_detection_pipeline()

    if mode == "all":
        run_detection_pipeline()
