import argparse
import getpass
import sys
from pathlib import Path


def _alembic_config():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return cfg


def migrate(revision: str = "head") -> None:
    from alembic import command

    command.upgrade(_alembic_config(), revision)


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(_alembic_config()).get_current_head()


def config_problems() -> list[str]:
    from ledger.config import get_settings

    s = get_settings()
    problems = []
    secret = s.session_secret.get_secret_value()
    if secret == "dev-only-change-me" or len(secret) < 32:
        problems.append("SESSION_SECRET must be set to a random value of at least 32 characters")
    if not s.app_password_hash:
        problems.append("APP_PASSWORD_HASH is not set (create one with: ledger hash-password)")
    return problems


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hash-password", help="Create an APP_PASSWORD_HASH value")
    m = sub.add_parser("migrate", help="Apply database migrations")
    m.add_argument("revision", nargs="?", default="head")
    rev = sub.add_parser("revision", help="Autogenerate a migration")
    rev.add_argument("-m", "--message", required=True)
    rn = sub.add_parser("renormalize", help="Recompute merchant keys and fingerprints for all transactions")
    rn.add_argument("--dry-run", action="store_true", help="Report what would change without saving")
    s = sub.add_parser("serve", help="Run migrations then start the web server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-migrate", action="store_true")
    s.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "hash-password":
        from ledger.auth import hash_password

        pw = getpass.getpass("Password: ")
        if pw != getpass.getpass("Confirm: "):
            sys.exit("Passwords do not match")
        print("Add this line to .env (single quotes keep docker compose from expanding the $ signs):")
        print(f"APP_PASSWORD_HASH='{hash_password(pw)}'")
    elif args.cmd == "migrate":
        migrate(args.revision)
    elif args.cmd == "revision":
        from alembic import command

        command.revision(_alembic_config(), message=args.message, autogenerate=True)
    elif args.cmd == "renormalize":
        import asyncio

        from ledger.db.engine import dispose_engine, get_sessionmaker
        from ledger.maintenance import renormalize

        async def run() -> dict:
            async with get_sessionmaker()() as session:
                result = await renormalize(session, dry_run=args.dry_run)
            await dispose_engine()
            return result

        r = asyncio.run(run())
        prefix = "dry run, nothing saved: would have " if args.dry_run else ""
        print(
            f"{prefix}merged {r['merged']} merchants into their new keys, aliased {r['aliased']} new keys to existing "
            f"merchants, updated {r['updated']} transactions"
        )
    elif args.cmd == "serve":
        import uvicorn

        if problems := config_problems():
            sys.exit("Refusing to start:\n- " + "\n- ".join(problems))
        if not args.no_migrate:
            migrate()
        uvicorn.run("ledger.main:app", host=args.host, port=args.port, reload=args.reload, proxy_headers=True)


if __name__ == "__main__":
    main()
