"""Crash-safe file writes, shared by every PDF/asset writer.

atomic_write guarantees two things the scattered copies it replaced only
half-delivered:
- crash safety: the final path only ever holds complete bytes (tmp file +
  os.replace), so a crash mid-write never leaves a truncated file; and
- concurrent-writer safety: the tmp name embeds pid/thread/random, so two
  requests writing the same destination (e.g. staff uploading a wet-signed
  contract PDF while the customer e-signs) never truncate each other's
  in-flight tmp file. Last os.replace still wins the destination — arbitrating
  WHO may write is the caller's job (status guards) — but the loser can no
  longer corrupt the winner mid-write.
"""
import os
import secrets
import threading


def atomic_write(path: str, data: bytes) -> None:
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    finally:
        # A failed replace (or a write error) must not strand the tmp file for
        # the backup sweep to find.
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
