# Antigravity reviewer

Antigravity CLI (`agy`) is the supported Google subscription-backed reviewer.
The legacy Gemini CLI binary is not used by this plugin.

## Install and authenticate

Install the Homebrew cask:

```bash
brew install --cask antigravity-cli
```

Run `agy`, choose Google OAuth, complete onboarding, and trust only the
repository directories that need review. Do not trust an entire home directory
for convenience.

Confirm authenticated access without spending inference tokens:

```bash
agy models
```

The command must exit successfully and return at least one model. The runner
uses this as its readiness probe and blocks a review before snapshot creation
when the probe fails.

## Review execution

The adapter runs a fresh non-interactive session with:

```text
agy --agent codex-multi-model-review-read-only-v1 \
  --mode plan --sandbox --output-format json \
  --add-dir <provider-specific-input-directory> --print <review-prompt>
```

The added directory contains only the staged patch, manifest, and prompt. The
private artifact directory containing peer reports, metadata, and Codex triage
is never granted to the reviewer.

Install the bundled custom agent once:

```bash
mm-review install-antigravity-agent
```

It is stored under Antigravity's global custom-agent directory but remains
inactive unless the runner selects it with `--agent`. Its hard tool allowlist
contains only `view_file` and `grep_search`, and its
`commandExecutionPolicy` is `off`. The runner verifies the installed agent
against the bundled SHA-256 before every readiness check and fails closed when
it is missing, changed, or symlinked. Enabling Antigravity also refreshes it.

Plan mode and the OS sandbox add defense in depth. The review prompt also
forbids terminal commands because headless mode cannot approve them safely.
Never add `--dangerously-skip-permissions` and do not broadly allow
`command(*)` just to make a review complete.

Model `auto` omits `--model` and lets Antigravity route the request. When an
explicit model is configured, the runner first verifies that the exact name is
present in `agy models`.

The adapter persists the Markdown response, private raw JSON, duration, turn
count, and token usage with the rest of the immutable review artifacts.

## Compatibility

Existing configuration under the old `gemini` key migrates in memory to
`antigravity`. The CLI accepts `gemini` provider names and `--with-gemini`,
`--without-gemini`, and `--gemini-model` as compatibility aliases, but new
automation should use the Antigravity names.
