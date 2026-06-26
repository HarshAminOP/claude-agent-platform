# Build & Distribution

This guide covers two workflows:

1. **You're building CAP** — how to produce the installable package from source
2. **You're sharing CAP with others** — how teammates install it on their machines

---

## Step 1: Build

You need the source code and `uv` installed.

```bash
# Clone the repository
git clone git@github.com:moia-dev/claude-agent-platform.git
cd claude-agent-platform

# Build the package
uv build
```

That's it. You now have a `.whl` file in `dist/`:

```
dist/claude_agent_platform-0.3.0-py3-none-any.whl
```

This single file contains everything — CLI, agents, workflows, eval framework, config defaults. No other files are needed.

---

## Step 2: Share

Pick whichever method fits your team:

### Option A: Send the file

Share `dist/claude_agent_platform-0.3.0-py3-none-any.whl` via Slack, Teams, email, shared drive, S3 — whatever you use to share files internally.

### Option B: Install directly from Git

Skip the build step entirely. Your teammate runs:

```bash
uv tool install "git+ssh://git@github.com/moia-dev/claude-agent-platform.git@v0.3.0"
```

This clones, builds, and installs in one command. Replace `v0.3.0` with the tag or branch you want.

---

## Step 3: Install (for the person receiving it)

### Prerequisites

Your teammate needs three things:

| What | How to get it |
|:-----|:--------------|
| **Python 3.11+** | `brew install python@3.12` (macOS) or system package manager |
| **uv** | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Claude Code** | [docs.anthropic.com/en/docs/claude-code](https://docs.anthropic.com/en/docs/claude-code/overview) |

### Install from the wheel file

```bash
uv tool install ./claude_agent_platform-0.3.0-py3-none-any.whl
```

### Or install from Git (no wheel file needed)

```bash
uv tool install "git+ssh://git@github.com/moia-dev/claude-agent-platform.git@v0.3.0"
```

### Initialize

After install, regardless of method:

```bash
cap init       # Creates databases, installs agents & workflows, registers MCP servers
cap status     # Verify everything is healthy
```

**Done.** Next time your teammate opens Claude Code, CAP is active.

---

## Step 4: Configure (optional)

By default CAP works immediately with keyword search + knowledge graph. For **semantic search** (finding conceptually related content), configure AWS Bedrock access:

```bash
# Edit the config file
open ~/.claude-platform/config.toml
```

Set your AWS profile and region:

```toml
[bedrock]
region = "eu-central-1"
profile = "your-sso-profile"
```

Then authenticate:

```bash
aws sso login --profile your-sso-profile
```

See [CONFIGURATION.md](CONFIGURATION.md) for all available settings.

---

## Upgrading

When a new version is available:

```bash
# From a new wheel file
uv tool install ./claude_agent_platform-0.4.0-py3-none-any.whl --force

# Or from Git
uv tool install "git+ssh://git@github.com/moia-dev/claude-agent-platform.git@v0.4.0" --force

# Then re-initialize (preserves your databases, updates agents/workflows/servers)
cap init --force
```

Your knowledge base, session memory, and learnings carry forward across versions.

---

## Uninstalling

```bash
cap uninstall --yes                      # Removes CAP config, restores original Claude settings
uv tool uninstall claude-agent-platform  # Removes the package
```

System returns to exactly how it was before installation.

---

## Complete End-to-End Example

**Person building and sharing:**

```bash
# 1. Get the source
git clone git@github.com:moia-dev/claude-agent-platform.git
cd claude-agent-platform

# 2. Build the wheel
uv build

# 3. Share it (pick one)
#    - Slack: drag dist/claude_agent_platform-0.3.0-py3-none-any.whl into a channel
#    - S3:    aws s3 cp dist/*.whl s3://team-tools/cap/
#    - Git:   just tell them the repo URL and tag
```

**Person installing:**

```bash
# 1. Install prerequisites (one-time)
curl -LsSf https://astral.sh/uv/install.sh | sh   # Install uv
source ~/.zshrc                                     # Reload shell

# 2. Install CAP (pick one — from file, S3, or Git)
uv tool install ./claude_agent_platform-0.3.0-py3-none-any.whl
# OR: uv tool install "git+ssh://git@github.com/moia-dev/claude-agent-platform.git@v0.3.0"

# 3. Initialize
cap init

# 4. Verify
cap status
cap doctor

# 5. (Optional) Set up AWS for semantic search
# Edit ~/.claude-platform/config.toml → [bedrock] section

# 6. Use Claude Code normally — CAP is now active
```

---

## Troubleshooting

### "cap: command not found"

`uv tool install` puts binaries in `~/.local/bin/`. Add to your shell config:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### "MCP server failed to start"

```bash
cap doctor              # Diagnose issues
cap fleet health-check  # Check individual server status
```

### Python version issues

Force a specific Python version:

```bash
uv tool install claude-agent-platform --python 3.12
```

### Reinstall from scratch

```bash
cap uninstall --yes
uv tool uninstall claude-agent-platform
uv tool install ./claude_agent_platform-0.3.0-py3-none-any.whl
cap init --force
```

---

## CI/CD (automated builds)

For teams that want to build automatically on tag:

```yaml
# .github/workflows/build.yml
name: Build
on:
  push:
    tags: ['v*']

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv build
      - uses: actions/upload-artifact@v4
        with:
          name: cap-wheel
          path: dist/*.whl
```

---

## Verifying a Build

Confirm the wheel is complete before distributing:

```bash
python -c "
import zipfile
z = zipfile.ZipFile('dist/claude_agent_platform-0.3.0-py3-none-any.whl')
names = z.namelist()
checks = {
    'CLI':       any('cap/cli/main.py' in n for n in names),
    'Agents':    sum(1 for n in names if 'cap/data/agents/' in n and n.endswith('.md')),
    'Workflows': sum(1 for n in names if 'cap/data/workflows/' in n and n.endswith('.js')),
    'Eval':      any('cap/eval/framework.py' in n for n in names),
    'Config':    any('config.toml.default' in n for n in names),
}
for k, v in checks.items():
    print(f'  {k}: {v}')
"
```

Expected output:

```
  CLI: True
  Agents: 14
  Workflows: 10
  Eval: True
  Config: True
```

---

## Related Documentation

| Doc | Link |
|:----|:-----|
| Installation details | [INSTALL.md](INSTALL.md) |
| Configuration options | [CONFIGURATION.md](CONFIGURATION.md) |
| Usage guide | [USAGE.md](USAGE.md) |
| Technical reference | [TECHNICAL.md](TECHNICAL.md) |

---

*Back to [README](../README.md)*
