# Development

Working on the server itself. To *use* it, the README's install steps are all you need.

## Set up

```bash
git clone https://github.com/konstantinos2018/cdse-local-mcp
cd cdse-local-mcp
uv sync                    # or: pip install -e .
uv run pytest              # unit tests, no network
uv run pytest -m live      # against the real CDSE API; needs no credentials
uv run ruff check . && uv run mypy
```

**Run your working copy in a client** by pointing it at the executable `uv sync` installed,
instead of at `uvx`. It is installed in editable mode, so your changes take effect the next
time the client restarts the server:

```bash
realpath .venv/bin/cdse-local-mcp     # use this as "command"; Windows: .venv\Scripts\cdse-local-mcp.exe
```

**Exercise the tools by hand** with the MCP Inspector. Run it from the repository directory,
where a `.env` file is read; copy [.env.example](../.env.example) to start one:

```bash
npx @modelcontextprotocol/inspector uv run cdse-local-mcp
```

**Test through a real client** with the scenarios in
[tests/manual/scenarios.md](../tests/manual/scenarios.md). Record each session with the
*Manual test run* issue template.

## Design documents

[CLAUDE.md](../CLAUDE.md) for working conventions,
[docs/cdse-apis.md](cdse-apis.md) for verified endpoints and quotas,
[docs/collections.md](collections.md) for per-collection detail,
[docs/downloads.md](downloads.md) for the download architecture,
[docs/design.md](design.md) for the user-facing behaviour and its rationale.

## Conventions

[CLAUDE.md](../CLAUDE.md) holds the working rules, and [CONTRIBUTING.md](../CONTRIBUTING.md)
the commit format. Run `ruff`, `mypy` and `pytest` before calling a change done.
