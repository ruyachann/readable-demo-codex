from .cli import main

if __name__ == "__main__":
    from .codex_assist import register
    register()
    raise SystemExit(main())
