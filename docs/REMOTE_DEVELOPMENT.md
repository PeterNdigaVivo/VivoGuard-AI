# Remote development

GitHub Codespaces provides a remote development workspace for this repository.
The repository configuration installs the API and frontend development
dependencies, enables Docker Compose on demand, and privately forwards the
frontend and API development ports.

## Create a workspace

1. Open the repository on GitHub.
2. Select **Code**, then **Codespaces**, then **Create codespace on** the branch
   you intend to work on.
3. Wait for the post-create dependency installation to finish.
4. Create a personal feature branch before editing:

   ```bash
   git switch -c codex/<short-name>
   ```

Use a separate codespace and feature branch for each contributor. Exchange
changes through commits and pull requests rather than sharing one working tree.

## Development commands

Frontend:

```bash
npm --prefix frontend run dev -- --host 0.0.0.0
```

API tests and lint:

```bash
python -m pytest backend/tests
python -m ruff check backend/app backend/tests
```

The full stack can be built with Docker Compose when a suitably sized
Codespaces machine is available. Do not start it with the production `.env`.
Copy `.env.example` to `.env` and use development-only secrets and endpoints.

## Security and production boundary

- Never add production camera, NVR, Odoo, SMTP, Twilio, webhook, or API
  credentials to the repository or a codespace image.
- Store any required development secret in Codespaces secrets, scoped to this
  repository, and keep integrations disabled unless the test explicitly needs
  them.
- Keep forwarded ports private. Do not make a camera stream, API, or dashboard
  port public.
- A codespace is a development environment. Pushing a branch or opening a pull
  request does not deploy code or change VivoGuard production configuration.

## Collaboration boundary

Each contributor should use their own GitHub and OpenAI identities. Grant the
colleague repository access, then have them create their own codespace and
branch. Do not share account passwords, browser sessions, tokens, or API keys.
