# {{APP_NAME}}

ESP-IDF 5.5 firmware for `{{TARGET}}` managed over the air by [Fleetforge](https://github.com/tvaroska/fleetforge) (project `{{PROJECT_SLUG}}`, partition layout `ab-4m-v1`).

## Quickstart

1. **Configure credentials (`.env`):**
   ```sh
   cp .env.example .env
   # Edit .env and set FLEETFORGE_TOKEN=ffa_...
   ```

2. **Build firmware (`build/{{APP_NAME}}.bin`):**
   ```sh
   just build        # local Docker (espressif/idf:v5.5.5)
   # or:
   just cloud-build  # remote Google Cloud Build (no local Docker required)
   ```

3. **Upload & deploy over the air:**
   ```sh
   just push --deploy dev
   ```

4. **Check fleet status, tail logs, and manage rotating secrets:**
   ```sh
   just status
   just logs
   just secrets list
   just secrets set api_url https://example.com/api
   ```

See [`AGENTS.md`](AGENTS.md) for the complete Fleetforge C API reference, rollback safety rules, and hardware DRAM budget.
