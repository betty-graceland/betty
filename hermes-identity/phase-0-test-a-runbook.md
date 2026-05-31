# Phase 0 — Test A — MCP hello-world runbook

Proves the OpenClaw → Hermes MCP bridge end-to-end. If Betty (Qwen via
Hermes) can call `mcp_betty_parse_airbnb_dossier` and get back a valid
Stays dict, the architecture lock holds and Phase 1 unlocks.

Expected runtime: 15–25 minutes including verification.

---

## Step 1 — pull the new MCP server code on Betty's Mac

```
cd ~/code/betty
git pull
cat claw/betty_claw/mcp_server.py | head -5
```

The `cat` should show the new module's docstring. If you see "Phase 4.7 — OpenClaw MCP server", the pull worked.

## Step 2 — install the new `mcp` dependency

```
cd ~/code/betty
uv sync
```

This installs `mcp>=1.26.0` into the betty workspace's venv. `uv sync` reads the updated `claw/pyproject.toml`, resolves, and writes `uv.lock`. Expect ~10 seconds.

If you see `Resolved N packages` and `Installed mcp-1.x.x`, you're good. If `uv sync` errors with a Python version mismatch, the env was built against a different Python — fix with `uv venv --python 3.12`.

## Step 3 — smoke-test the server standalone

Before involving Hermes, verify the server starts cleanly:

```
cd ~/code/betty
uv run python -m betty_claw.mcp_server
```

You should see (on stderr):

```
[betty_claw.mcp_server] 2026-05-27 ... INFO OpenClaw MCP server starting (Phase 4.7 Phase 0 prototype)
[betty_claw.mcp_server] 2026-05-27 ... INFO Exposing 2 tools: parse_airbnb_dossier, betty_ping
```

Then it blocks waiting for stdio input. Press **Ctrl+C** to exit. If you see this, the server-side is healthy.

If you see import errors or `ModuleNotFoundError: No module named 'mcp'`, `uv sync` didn't install the dep. Re-run step 2.

## Step 4 — register the server in Hermes config

Open Hermes config:

```
nano ~/.hermes/config.yaml
```

Find the top level (or scroll to the end). Add this block. **Mind the indentation — YAML is whitespace-significant.** Add it as a top-level key (column 1):

```yaml
mcp_servers:
  betty:
    command: "uv"
    args:
      - "run"
      - "--directory"
      - "/Users/betty/code/betty"
      - "python"
      - "-m"
      - "betty_claw.mcp_server"
    env:
      BETTY_SITE_DIR: "/Users/betty/Projects/emdash/travelpec-site"
      BETTY_DOCS_DIR: "/Users/betty/My Drive/Betty/emdash-sites/travelpec.com-v3"
      BETTY_RESEARCH_DIR: "/Users/betty/travelpec-com"
    timeout: 60
    connect_timeout: 30
```

If `mcp_servers:` already exists in your config (it shouldn't yet), just add the `betty:` block under it indented one level.

Save (Ctrl+O, Enter, Ctrl+X in nano).

Verify the YAML parses by running:

```
hermes config show 2>&1 | head -40
```

If you see a YAML error, the indentation drifted. The Hermes docs note YAML indentation is the #1 troubleshooting cause; if the show command fails, paste the error and we fix.

## Step 5 — restart Hermes

```
# If hermes is running interactively, type:
/exit

# Then:
hermes
```

On startup Hermes will:
- Spawn the betty MCP server subprocess via the configured command
- Call `tools/list` to discover what tools are exposed
- Register `mcp_betty_parse_airbnb_dossier` and `mcp_betty_betty_ping` in its tool registry
- Inject them into every conversation toolset

Watch the startup logs for one of these lines:

- `Connected to MCP server 'betty'` (success — proceed to step 6)
- `Failed to connect to MCP server 'betty'` (failure — capture the error)

## Step 6 — verify tools are registered

Inside the Hermes CLI:

```
> Betty, list every tool whose name starts with mcp_betty
```

Expected reply: she lists `mcp_betty_parse_airbnb_dossier` and `mcp_betty_betty_ping` (and explains they're newly available via the OpenClaw MCP server).

If she replies with no `mcp_betty_*` tools, the MCP discovery failed. Capture Hermes startup logs.

## Step 7 — test the ping

```
> Betty, call the mcp_betty_betty_ping tool and tell me what it returns.
```

Expected reply: she calls the tool, gets back `{server: "betty", status: "alive", phase: "4.7.0 / Phase 0 prototype", message: "OpenClaw MCP server is reachable from Hermes."}`. She paraphrases or quotes it.

If ping works, the MCP transport is proven on a trivial tool. If it fails but tools were registered in step 6, something is broken in subprocess execution; check Hermes logs for the actual error.

## Step 8 — test the parser (the real Test A)

```
> Betty, call mcp_betty_parse_airbnb_dossier with the path /Users/betty/travelpec-com/01-source-data/research/airbnb-listings/3_Bed_PEC_Home_Loads_of_Style_12_hr_to_Sandbanks.md and summarize the parsed Stays data.
```

Expected reply: she calls the tool, gets back the full payload (data + frontmatter + body_excerpt + summary), and summarizes the Stays fields — title="3 Bed PEC Home...", village="Sandbanks", persona about the Parsonage, bedrooms=3.0, capacity=6.0, etc.

If she gets the data and summarizes it correctly, **Test A passes** and Phase 0's first seam is proven.

## Step 9 — record what worked

```
echo "Test A passed: $(date)" >> ~/code/betty/phase-0-results.txt
```

Then paste the full Telegram or CLI transcript of steps 6-8 back to me. I'll use it as evidence the architecture lock holds.

---

## Failure modes and what to capture

If anything fails, paste:

1. The exact step number that failed
2. The full error message (Hermes logs at `~/.hermes/logs/` if available, plus stderr from the MCP subprocess if visible)
3. Output of `hermes config show 2>&1 | grep -A 20 mcp_servers` (confirms the config registered)
4. Output of `which uv && uv --version` (confirms uv is on PATH where Hermes can find it)

Common failures and fixes:

- **"command 'uv' not found"**: Hermes's environment doesn't have `~/.local/bin` (where uv lives) on PATH. Either add it to Hermes's launch env via shell profile, or use the absolute path `/Users/betty/.local/bin/uv` in the config.
- **"Failed to connect to MCP server 'betty'": ModuleNotFoundError**: Step 2's `uv sync` didn't run, or it ran against the wrong project. Confirm `cd ~/code/betty && uv pip list | grep mcp` shows mcp installed.
- **"Failed to connect to MCP server 'betty'": timeout**: The subprocess started but didn't respond to the MCP handshake. Likely an exception during `mcp.run()` startup — run step 3 standalone to see the traceback.
- **Tools appear but `mcp_betty_betty_ping` returns an error**: The server is up but tool dispatch is broken; check stderr for the tool's exception.
