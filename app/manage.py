"""Command-line maintenance: `python -m app.manage <command>`.

  reset-owner [--username NAME] [--clear-2fa]
      Print a one-time link (valid 30 minutes) that sets a new password for an
      owner account -- the recovery path now that no password lives in the
      environment. Run it on the server, e.g.
      `docker compose exec app python -m app.manage reset-owner`.
"""
import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session


def reset_owner(username: str | None, clear_2fa: bool, base_url: str | None = None) -> int:
    from app import models, setup_flow
    from app.db import make_engine
    engine = make_engine()
    with Session(engine) as s:
        q = select(models.User).where(models.User.role == "admin", models.User.active)
        if username:
            q = q.where(models.User.username == username.strip().lower())
        owners = s.scalars(q.order_by(models.User.id)).all()
        if not owners:
            print("No active owner account" + (f" named {username!r}" if username else "") + ".",
                  file=sys.stderr)
            print("If no owner exists yet, open the app in a browser to run first-time setup.",
                  file=sys.stderr)
            return 1
        if len(owners) > 1 and not username:
            print("More than one owner account. Pick one with --username:", file=sys.stderr)
            for u in owners:
                print(f"  {u.username}  ({u.name})", file=sys.stderr)
            return 2
        user = owners[0]
        base_url = (base_url or models.get_setting(s, "public_base_url", "").strip()
                    or "http://localhost:8000")
        token = setup_flow.issue_reset(s, user.id, clear_2fa=clear_2fa)
        models.audit(s, None, "setup.owner_reset_issued", detail=user.username or "")
        who = f"{user.name} ({user.username})"
        s.commit()
    print(f"Reset link for {who}, valid for 30 minutes and usable once:")
    print(f"  {base_url.rstrip('/')}/setup/reset/{token}")
    if clear_2fa:
        print("Two-step sign-in will be turned off when the new password is saved.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.manage")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset-owner", help="print a one-time owner password reset link")
    r.add_argument("--username")
    r.add_argument("--clear-2fa", action="store_true",
                   help="also turn off two-step sign-in (lost phone and backup codes)")
    r.add_argument("--base-url", help="the address you open the app at (default: the public "
                   "address saved in Admin, else http://localhost:8000)")
    args = p.parse_args(argv)
    if args.cmd == "reset-owner":
        return reset_owner(args.username, args.clear_2fa, args.base_url)
    return 1


if __name__ == "__main__":
    sys.exit(main())
