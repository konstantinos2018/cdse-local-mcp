# Security policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue. Use the
**Report a vulnerability** button on this repository's
[Security tab](https://github.com/konstantinos2018/cdse-local-mcp/security/advisories/new).

Include what an attacker could do, how to reproduce it (the prompt or tool call), the version
or commit you tested, and which MCP client you used.

**Never include real credentials** in a report, yours or anyone else's. If you have exposed
your CDSE keys, revoke them in the
[S3 keys manager](https://eodata-s3keysmanager.dataspace.copernicus.eu/) and create new ones.

This is a small project maintained in spare time. Every report will be read and acknowledged,
but there is no guaranteed response time.

## Supported versions

The project has no stable release yet. Fixes go to the latest commit on `main`, which is what
the install instructions use.

## What matters most

The server acts on an AI assistant's instructions and holds download credentials, so these
are the problems most worth reporting:

- Reading, writing or deleting files **outside the download folder**, for example through a
  crafted product id, asset name or object key
- Credentials appearing in **tool results, logs or error messages**
- Credentials being sent **anywhere other than the Copernicus Data Space**
- Downloads that **bypass the transfer limits** (`CDSE_MAX_CALL_BYTES`,
  `CDSE_MAX_SESSION_BYTES`)

Out of scope here: problems in the Copernicus Data Space itself, in Claude or other MCP
clients, or in this project's dependencies. Please report those to their maintainers — but do
say so here if this project is exposed by one.
