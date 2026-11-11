#!/usr/bin/env python3
"""Print an argon2 hash for PORTAL_ADMIN_PASSWORD_HASH. The password is not saved."""
import getpass, sys
from argon2 import PasswordHasher

password = getpass.getpass("Owner password: ")
again = getpass.getpass("Repeat password: ")
if password != again:
    sys.exit("The two passwords did not match.")
if not 10 <= len(password) <= 200:
    sys.exit("Use 10 to 200 characters.")
print(PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1).hash(password))
