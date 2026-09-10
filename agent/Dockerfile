# syntax=docker/dockerfile:1.7
# =============================================================================
# The agent firmware builder. **Off-box only.**
# =============================================================================
# One pinned ESP-IDF builder, N per-target bundles. The upstream image already
# carries every target's toolchain, so building a separate builder per chip would
# duplicate ~5 GB for nothing (DECISIONS.md, R0-infra-2).
#
# THE PIN IS A DIGEST, and the tag next to it is documentation. Changing the IDF
# version changes firmware behaviour on every board flashed afterwards, so bumping it
# is a DECISIONS.md entry, not a chore — docs/runbooks/agent-build.md.
#
# Two outputs from one definition, which is why this is a `docker build` and not a
# bind-mounted `docker run`: `--output type=local,dest=agent/dist/<target>` writes the
# host bundle as the invoking user, and `-t …/fleetforge-agent-<target>` produces the
# tiny `FROM scratch` image pushed to Artifact Registry. A bind mount would write
# root-owned files into the repo (`-u $(id -u)` breaks IDF's export scripts, which need
# $HOME) and would let a stale host `sdkconfig` override sdkconfig.defaults.
#
#     just agent-build esp32       # bundle into agent/dist/esp32/
#     just agent-push  esp32       # same build, pushed as an OCI image
# =============================================================================

ARG IDF_IMAGE=espressif/idf:v5.5.5@sha256:a9231d0697ab8f7517cc072e93b7c83e04907bfbfba80b6440d7dbbf90665cf2

FROM ${IDF_IMAGE} AS builder

# The IDF toolchain is only on PATH after `. $IDF_PATH/export.sh` — that is what the
# upstream ENTRYPOINT does, and a bare `RUN idf.py build` fails with "idf.py: not found".
# `export.sh` is bash, so the default /bin/sh will not do either.
SHELL ["/bin/bash", "-c"]

ARG IDF_TARGET=esp32
ARG SOURCE_COMMIT=unknown
ARG IDF_IMAGE

WORKDIR /project
COPY . /project

# No `sdkconfig` exists in here (agent/.dockerignore excludes it), so SDKCONFIG_DEFAULTS
# is authoritative on every build — see the header of agent/sdkconfig.defaults.
RUN . $IDF_PATH/export.sh \
 && idf.py -B build -DIDF_TARGET=${IDF_TARGET} \
      -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.${IDF_TARGET}" \
      build \
 && python3 tools/make_manifest.py \
      --build-dir build --out /out --partitions partitions.csv \
      --source-commit "${SOURCE_COMMIT}" --idf-image "${IDF_IMAGE}"

# The published artefact is the bundle and nothing else: no toolchain, no layers, no
# base image to keep patched. ~1.2 MB in the registry, and a digest to quote.
FROM scratch AS export
COPY --from=builder /out/ /
