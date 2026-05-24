#!/usr/bin/env python3
"""Test whether yield from wrapper's finally runs on first-yield failure."""
import threading, time

sem = threading.Semaphore(1)
sem_acquired = False

def failing_gen():
    try:
        raise ValueError("error before any yield")
    finally:
        print("GEN FINALLY executed")

def wrapper(gen):
    try:
        yield from gen
    finally:
        print("WRAPPER FINALLY executed — releasing semaphore!")
        sem.release()

# Acquire semaphore (simulates execute_with_sem line 492)
sem.acquire()
sem_acquired = True
print(f"Semaphore acquired, internal value: {sem._value}")

# call_fn returns a generator that fails immediately
gen = failing_gen()

# Wrap it (simulates line 510)
wrapped = wrapper(gen)

# Caller tries to iterate (simulates line 555: next(it))
try:
    val = next(wrapped)
    print(f"Got value: {val}")
except ValueError as e:
    print(f"Exception caught by caller: {e}")
except Exception as e:
    print(f"Unexpected exception: {type(e).__name__}: {e}")

print("Code after try/except reached!")
time.sleep(0.3)

# Check if semaphore was released
released = threading.Event()
def try_acquire():
    sem.acquire()
    released.set()

t = threading.Thread(target=try_acquire)
t.start()
time.sleep(0.5)

if released.is_set():
    print("PASS: Semaphore was released (wrapper's finally ran)")
    sem.release()  # clean up
else:
    print("FAIL: Semaphore leaked — wrapper's finally did NOT run!")

t.join(timeout=1.0)
print(f"Final semaphore value: {sem._value}")