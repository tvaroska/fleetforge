# {{APP_NAME}}

ESP-IDF 5.5 firmware for `{{TARGET}}` managed over the air by [Fleetforge](https://github.com/tvaroska/fleetforge) (project `{{PROJECT_SLUG}}`, partition layout `ab-4m-v1`).

## Quickstart

1. **Authenticate with Fleetforge (once per workstation):**
   ```sh
   just login -u <username-or-email>
   just whoami
   ```
   *(Optional: for headless CI or per-repo overrides, copy `.env.example` to `.env` and set `FLEETFORGE_TOKEN=ffa_...`.)*

2. **Build firmware locally (`build/{{APP_NAME}}.bin`):**
   ```sh
   just build          # auto-detects host idf.py or falls back to local Docker (espressif/idf:v5.5.5)
   just build-idf      # host idf.py explicitly
   just build-docker   # Docker container explicitly
   ```

3. **Upload & deploy over the air (local or GitHub Actions):**
   ```sh
   just push --deploy dev
   ```
   Or push to `main` / tag `v*` on GitHub (with repository secret `FLEETFORGE_TOKEN` set) to run [`.github/workflows/fleetforge.yml`](.github/workflows/fleetforge.yml).

4. **Check fleet status, tail logs, and manage rotating secrets:**
   ```sh
   just status
   just logs
   just secrets list
   just secrets set api_url https://example.com/api
   ```

See [`AGENTS.md`](AGENTS.md) for the complete Fleetforge C API reference, rollback safety rules, and hardware DRAM budget.
